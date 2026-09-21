import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app.agent_model import OpenAICompatibleModel, InvalidStructuredOutput, MockAgentModel
from backend.app.network_policy import request_timeout, mcp_timeout, retry_delay
from backend.app.output_contracts import CONTRACTS
from backend.app.discovery_agent import SourceDiscoveryAgent
from backend.app.collection_pipeline import CollectionPipeline
from backend.app.collection_health import PROCESSING_VERSION, reopen_fixed_failure, item_diagnostic
from backend.app.publication_dates import publication_metadata, today_local
from backend.app.search_cache import SearchCache
from backend.app.case_context import evidence_case_context
from backend.app.reader_transport import fetch_reader, ReaderConnection, reader_addresses, validate_reader_target

ROOT = Path(__file__).resolve().parents[2]

def error(code, message):
    return urllib.error.HTTPError('https://model.test', code, 'error', {}, io.BytesIO(json.dumps({'error':{'message':message}}).encode()))

def response(data):
    return {'status':'completed','output_text':json.dumps(data)}

class ReliabilityTests(unittest.TestCase):
    def model(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY':'test-secret','AI_CASE_LLM_WIRE_API':'responses'}):
            m = OpenAICompatibleModel()
        m._working_wire = 'responses'
        return m

    def test_strict_schema_for_live_verification_and_feedback_on_missing_fields(self):
        m = self.model()
        m._post = Mock(side_effect=[response({'decision':'pass'}), response({'decision':'pass','reason':'evidence checked','limitations':[]})])
        result = m.structured('verification','check evidence',{'case':{},'article_text':'Original evidence'})
        self.assertEqual(result['decision'],'pass')
        body = m._post.call_args.args[1]
        self.assertTrue(body['text']['format']['strict'])
        self.assertIn('Schema mismatch',body['input'][-1]['content'])
        self.assertIn('reason',body['input'][-1]['content'])

    def test_explicit_unsupported_schema_falls_back_without_dropping_validation(self):
        m = self.model()
        m._post = Mock(side_effect=[error(400,'json_schema is not supported'), response({'decision':'pass','reason':'checked','limitations':[]})])
        m.structured('verification','check',{'article_text':'evidence'})
        self.assertEqual(m._post.call_args.args[1]['text']['format']['type'],'json_object')
        m._post = Mock(return_value=response({'decision':'yes'}))
        with self.assertRaises(InvalidStructuredOutput): m.structured('verification','check',{'article_text':'evidence'})
        self.assertEqual(m._post.call_count,2)

    def test_arbitrary_400_and_auth_never_retry_or_hide_original_reason(self):
        for code in (400,401,403):
            m = self.model(); m._post = Mock(side_effect=error(code,'invalid request content'))
            with self.assertRaisesRegex(RuntimeError,'invalid request content'):
                m.structured('verification','check',{})
            self.assertEqual(m._post.call_count,1)

    def test_secret_is_redacted_before_truncation(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-secret'}):
            detail = self.model()._http_error_detail(error(400,'key test-secret Bearer other-key refused'))
        self.assertNotIn('test-secret',detail); self.assertNotIn('other-key',detail)

    def test_search_only_downgrades_explicit_optional_parameter_and_keeps_tool_proof(self):
        m = self.model()
        m._post = Mock(side_effect=[error(400,'Unsupported parameter: max_tool_calls'),
            {'status':'completed','output':[{'type':'web_search_call','status':'completed',
            'action':{'sources':[{'url':'https://example.com/case','title':'Case'}]}}]}])
        self.assertEqual(m.search_web('AI workflow')[0]['url'],'https://example.com/case')
        self.assertNotIn('max_tool_calls',m._post.call_args.args[1])
        self.assertEqual(m._post.call_args.args[1]['tools'],[{'type':'web_search'}])
        m._post = Mock(side_effect=error(400,'Invalid value for reasoning'))
        with self.assertRaisesRegex(RuntimeError,'Invalid value'):m.search_web('AI workflow')
        self.assertEqual(m._post.call_count,1)

    def test_refusal_is_terminal_without_regeneration(self):
        m = self.model(); m._post = Mock(return_value={'status':'completed','output':[{'content':[{'type':'refusal','refusal':'declined'}]}]})
        with self.assertRaisesRegex(RuntimeError,'refused'):m.structured('verification','check',{})
        self.assertEqual(m._post.call_count,1)

    def test_chat_stream_requires_done_and_stop(self):
        chunks = [{'choices':[{'delta':{'content':'{"ok":true}'},'finish_reason':None}]}, {'choices':[{'delta':{},'finish_reason':'stop'}]}]
        stream = ''.join('data: '+json.dumps(x)+'\n\n' for x in chunks)
        with self.assertRaises(RuntimeError): OpenAICompatibleModel._parse_sse_result(stream)
        data = OpenAICompatibleModel._parse_sse_result(stream+'data: [DONE]\n\n')
        self.assertEqual(OpenAICompatibleModel._parse_json_result('chat',data),{'ok':True})
        with self.assertRaises(RuntimeError): OpenAICompatibleModel._parse_sse_result(stream.replace('stop','length')+'data: [DONE]\n\n')

    def test_schema_all_nested_fields_required_and_extra_forbidden(self):
        def check(node):
            if isinstance(node,dict):
                if node.get('type')=='object':
                    self.assertIs(node['additionalProperties'],False)
                    self.assertEqual(set(node['required']),set(node['properties']))
                for value in node.values():check(value)
            elif isinstance(node,list):
                for value in node:check(value)
        for model in CONTRACTS.values():check(model.model_json_schema())

    def test_review_context_keeps_date_provenance_without_prior_model_history(self):
        context = evidence_case_context({'published_at':'2026-06-09',
            'published_at_source':'html_meta:datepublished', 'published_at_evidence':'June 9, 2026',
            'quality_evaluation':{'review':{'reason':'a previous claim'}}, 'attempt_history':['old error']})
        self.assertEqual(context['published_at_evidence'],'June 9, 2026')
        self.assertEqual(context['published_at_source'],'html_meta:datepublished')
        self.assertNotIn('quality_evaluation',context);self.assertNotIn('attempt_history',context)

    def test_outer_mcp_budget_covers_two_search_attempts_and_backoff(self):
        with patch.dict(os.environ,{'AI_CASE_SEARCH_TIMEOUT':'90'}):
            self.assertGreater(mcp_timeout('search_web'),2*request_timeout(True)+8)
        e = error(429,'slow down');e.headers={'Retry-After':'900'}
        self.assertEqual(retry_delay(e),8)

    def test_reader_preserves_https_query_and_uses_longer_budget(self):
        url = 'https://writer.substack.com/p/my-case?language=en'
        alt = SourceDiscoveryAgent.fetch_profile(url)['alternatives'][0]
        self.assertEqual(alt,'https://r.jina.ai/'+url)
        r = Mock();r.headers.get_content_type.return_value='text/plain';r.read.return_value=b'Article body';r.__enter__=Mock(return_value=r);r.__exit__=Mock(return_value=False)
        with patch('backend.app.discovery_agent.validate_public_url') as guard, patch('backend.app.discovery_agent.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=r
            SourceDiscoveryAgent(ROOT)._fetch_bytes(alt,'text/html, text/plain')
            self.assertIn(unittest.mock.call(url),guard.call_args_list)
            self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'],45)

    def test_transient_fetch_retry_once_but_access_denial_not_retried(self):
        for status,expected in [(503,2),(403,1),(404,1)]:
            with patch('backend.app.discovery_agent.validate_public_url'), patch('backend.app.discovery_agent.time.sleep'), patch('backend.app.discovery_agent.urllib.request.build_opener') as opener:
                opener.return_value.open.side_effect=error(status,'failed')
                with self.assertRaises(urllib.error.HTTPError):SourceDiscoveryAgent(ROOT)._fetch_bytes('https://example.com','text/html')
                self.assertEqual(opener.return_value.open.call_count,expected)

    def test_reader_dns_recovery_keeps_private_article_targets_blocked(self):
        agent=SourceDiscoveryAgent(ROOT)
        reader='https://r.jina.ai/https://example.com/article'
        with patch('backend.app.discovery_agent.validate_public_url',side_effect=[None,ValueError('Private/local addresses are not article sources')]), patch('backend.app.discovery_agent.fetch_reader',return_value=(b'public article','text/plain')) as fetch:
            self.assertEqual(agent._fetch_bytes(reader,'text/plain'),b'public article')
            fetch.assert_called_once()
        with patch('backend.app.discovery_agent.validate_public_url',side_effect=ValueError('Private/local addresses are not article sources')), patch('backend.app.discovery_agent.fetch_reader') as fetch:
            with self.assertRaises(ValueError):agent._fetch_bytes('https://r.jina.ai/http://127.0.0.1/secret','text/plain')
            fetch.assert_not_called()
        with self.assertRaises(ValueError):ReaderConnection('127.0.0.1',10)
        with self.assertRaises(ValueError):fetch_reader('https://other.example/path',{},10,100)

    def test_reader_public_dns_rejects_private_answers(self):
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps({'Status':0,'Answer':[{'name':'r.jina.ai.','type':1,'data':'192.168.1.1'}]}).encode()
        with patch('backend.app.reader_transport.urllib.request.urlopen',return_value=response):
            with self.assertRaises(ValueError):reader_addresses()

    def test_polluted_original_dns_only_recovers_through_verified_public_reader(self):
        agent = SourceDiscoveryAgent(ROOT)
        page = b'Title: Test\nPublished Time: 2026-03-28\nMarkdown Content:\n' + b'public original evidence ' * 30
        with patch.object(agent, '_fetch_bytes', side_effect=[ValueError('Private/local addresses are not article sources'), (page,'text/plain')]), patch('backend.app.discovery_agent.validate_reader_target') as verify:
            result = agent.fetch_page('https://writer.substack.com/p/public-case')
            verify.assert_called_once_with('https://writer.substack.com/p/public-case')
            self.assertTrue(result['fetched_url'].startswith('https://r.jina.ai/'))
        for url in ('http://127.0.0.1/x', 'http://192.168.1.1/x', 'http://metadata.internal/x', 'http://localhost/x'):
            with patch('backend.app.reader_transport.urllib.request.urlopen') as network:
                with self.assertRaises(ValueError):validate_reader_target(url)
                network.assert_not_called()

    def test_challenge_page_never_becomes_article(self):
        agent = SourceDiscoveryAgent(ROOT)
        page = b'<title>Just a moment</title><p>Verify you are human.</p>' * 20
        with patch.object(agent,'_fetch_bytes',return_value=(page,'text/html')):
            with self.assertRaisesRegex(RuntimeError,'challenge page'):agent.fetch_page('https://example.com/case')

    def test_reader_transport_headers_are_not_article_evidence(self):
        text,title = SourceDiscoveryAgent._extract_document(b'Title: Real workflow\nURL Source: https://example.com\nPublished Time: 2026-01-01\nMarkdown Content:\nI used AI to deliver a report.','text/plain','https://r.jina.ai/https://example.com')
        self.assertEqual(title,'Real workflow');self.assertEqual(text,'I used AI to deliver a report.')

    def test_simon_footer_requires_matching_article_path(self):
        html = b'<div class="entryFooter">Posted <a href="/2026/Jan/12/">12th January 2026</a> at 9:46 pm</div>'
        self.assertEqual(publication_metadata(html,'text/html','https://simonwillison.net/2026/Jan/12/story')['published_at'],'2026-01-12')
        self.assertNotIn('published_at',publication_metadata(html,'text/html','https://simonwillison.net/2026/'))

    def test_cache_does_not_reuse_expired_or_empty_results(self):
        with tempfile.TemporaryDirectory() as d:
            cache=SearchCache(d,ttl=10); cache.put('query',5,[{'url':'https://source.example','provider':'responses.web_search'}])
            self.assertEqual(len(cache.get('query',5)),1);self.assertIsNone(cache.get('different query',5))
            with patch('backend.app.search_cache.time.time',return_value=10**12):self.assertIsNone(cache.get('query',5))
            cache.put('empty',5,[]);self.assertIsNone(cache.get('empty',5))

    def test_fixed_exhausted_model_failure_reopens_only_once(self):
        item={'attempt_count':3,'status':'needs_extraction','processing_version':'source-access-v2','extraction':{'extraction_status':'model_unavailable','reason':'HTTP 400'}}
        self.assertTrue(reopen_fixed_failure(item));self.assertEqual(item['attempt_count'],0)
        self.assertEqual(item['processing_version'],PROCESSING_VERSION);self.assertFalse(reopen_fixed_failure(item))

    def test_reader_timeout_after_direct_403_is_not_classified_as_permanent_block(self):
        diagnostic = item_diagnostic({'status':'needs_extraction','extraction':{
            'extraction_status':'fetch_failed','reason':'direct[example.com]: HTTPError: HTTP 403; newsletter_platform[r.jina.ai]: TimeoutError: timed out'}})
        self.assertEqual(diagnostic['code'],'fetch_failed')
        self.assertEqual(diagnostic['label'],'原文读取超时')

    def test_two_search_failures_stop_remaining_branches_and_preserve_existing_work(self):
        with tempfile.TemporaryDirectory() as d:
            pipeline=CollectionPipeline(ROOT,Path(d)/'catalog.db',model=MockAgentModel())
            try:
                pipeline.discovery.catalog_feed_urls=Mock(return_value=[])
                pipeline.discovery.search_branches=Mock(return_value=[{'id':str(i),'label':str(i),'query':str(i)} for i in range(4)])
                pipeline.live_source.call=Mock(side_effect=RuntimeError('gateway timeout'))
                r=pipeline.run('live',include_catalog=True)
                self.assertEqual(pipeline.live_source.call.call_count,2)
                self.assertTrue(any(e['action']=='search.circuit_open' for e in r['events']))
            finally:pipeline.close()

    def test_model_outage_stops_batch_without_consuming_other_items(self):
        with tempfile.TemporaryDirectory() as d:
            model=Mock();model.structured.side_effect=RuntimeError('LLM request failed: HTTP 503')
            pipeline=CollectionPipeline(ROOT,Path(d)/'catalog.db',model=model)
            try:
                for i in range(5):pipeline.db.save_source_item({'case_id':str(i),'source_url':f'https://example.com/{i}','discovery_mode':'web_search','status':'needs_extraction'},'needs_extraction')
                pipeline.live_source.call=Mock(return_value={'text':'Original public article. '*20,'content_hash':'hash','published_at':today_local().isoformat()})
                r=pipeline.run('live',search_web=False,retry_pending=True,max_results=12)
                self.assertEqual(r['processed_count'],2);self.assertEqual(r['queued'],3)
                self.assertEqual(r['stop_reason'],'model_service_unavailable')
            finally:pipeline.close()

    def test_targeted_retry_never_processes_unrequested_records(self):
        with tempfile.TemporaryDirectory() as d:
            pipeline=CollectionPipeline(ROOT,Path(d)/'catalog.db',model=MockAgentModel())
            try:
                for id in ('approved', 'unrelated'):
                    pipeline.db.save_source_item({'case_id':id,'source_url':f'https://example.com/{id}',
                        'discovery_mode':'web_search','status':'needs_extraction'},'needs_extraction')
                pipeline.live_source.call=Mock(side_effect=RuntimeError('fetch failed'))
                r=pipeline.run('live',search_web=False,retry_pending=True,retry_case_ids=['approved'])
                self.assertEqual([i['id'] for i in r['items']],['approved'])
                self.assertEqual(pipeline.live_source.call.call_count,1)
                with self.assertRaises(ValueError):pipeline.run('live',retry_pending=True,retry_case_ids=['approved'])
            finally:pipeline.close()

    def test_large_ready_backlog_stops_new_discovery_and_skips_automatic_feeds(self):
        with tempfile.TemporaryDirectory() as d:
            pipeline=CollectionPipeline(ROOT,Path(d)/'catalog.db',model=MockAgentModel())
            try:
                for i in range(24):
                    pipeline.db.save_source_item({'case_id':str(i),'source_url':f'https://example.com/{i}',
                        'discovery_mode':'web_search','status':'needs_extraction'},'needs_extraction')
                pipeline.discovery.search_branches=Mock()
                pipeline.discovery.catalog_feed_urls=Mock()
                pipeline.live_source.call=Mock(side_effect=lambda tool,**kwargs: [] if tool=='search_web' else (_ for _ in ()).throw(RuntimeError('fetch failed')))
                r=pipeline.run('live',retry_pending=True,include_catalog=True)
                searches=[c for c in pipeline.live_source.call.call_args_list if c.args[0]=='search_web']
                self.assertEqual(len(searches),0)
                self.assertEqual(r['processed_count'],12)
                self.assertEqual(r['discovery_budget'],0)
                self.assertEqual(r['discovery_policy'],'capacity_matched')
                pipeline.discovery.search_branches.assert_not_called()
                pipeline.discovery.catalog_feed_urls.assert_not_called()
            finally:pipeline.close()
