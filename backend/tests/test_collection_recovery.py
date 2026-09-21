import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app.agent_model import MockAgentModel, OpenAICompatibleModel
from backend.app.collection_pipeline import CollectionPipeline
from backend.app.collection_health import prioritize_candidates, summarize_outcomes, reopen_fixed_failure, retry_ready
from backend.app.evidence_catalog import build_evidence_catalog
from backend.app.extraction_agent import ExtractionAgent
from backend.app.discovery_agent import SourceDiscoveryAgent
from backend.app.publication_dates import today_local

ROOT = Path(__file__).resolve().parents[2]


class RecoveryTests(unittest.TestCase):
    def test_catalog_keeps_sentences_and_exact_offsets(self):
        text = ('A complete sentence about a real working AI tool. ' * 35) + '最后一句完整的原文。'
        catalog = build_evidence_catalog(text)
        self.assertEqual(''.join(x['text'] for x in catalog.values()), text)
        for span in catalog.values():
            self.assertEqual(span['text'], text[span['start']:span['end']])
            self.assertIn(span['text'].rstrip()[-1], '.。')

    def test_extraction_resolves_ids_without_changing_original_quote(self):
        text = 'We used AI to complete our reporting task and published the finished report.'
        model = Mock()
        model.structured.return_value = {key: {'claim': text, 'evidence_ids': ['E001']} for key in ('problem', 'approach', 'outcome')}
        result = ExtractionAgent().extract_case({}, {'text': text}, model)
        self.assertEqual(result['extraction_status'], 'structured_claims')
        self.assertEqual(result['outcome']['evidence'], text)
        model.structured.return_value['outcome']['evidence_ids'] = ['invented']
        self.assertEqual(ExtractionAgent().extract_case({}, {'text': text}, model)['extraction_status'], 'model_rejected')

    def test_non_case_is_not_treated_as_transport_failure(self):
        model = Mock()
        model.structured.return_value = {'case_decision': 'not_case', 'reason': 'Only a proposed tutorial; no actual outcome.', 'evidence_ids': ['E001']}
        result = ExtractionAgent().extract_case({}, {'text': 'You could use AI for this task in the future.'}, model)
        self.assertEqual(result['extraction_status'], 'not_case')
        self.assertEqual(model.structured.call_count, 1)

    def test_retry_scans_past_exhausted_rows_and_does_not_rediscover_rss(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = CollectionPipeline(ROOT, Path(directory) / 'catalog.db', model=MockAgentModel())
            try:
                for index in range(22):
                    item = {'case_id': str(index), 'source_url': f'https://example.com/{index}', 'title_original': str(index),
                            'status': 'needs_extraction', 'discovery_mode': 'web_search', 'attempt_count': 3 if index < 21 else 0}
                    pipeline.db.save_source_item(item, 'needs_extraction')
                pipeline.discovery.catalog_feed_urls = Mock(side_effect=AssertionError('retry must not fetch fresh feeds'))
                pipeline.live_source.call = Mock(return_value={'text': 'Safe article ' * 50, 'title': 'Article', 'content_hash': 'hash', 'published_at': today_local().isoformat()})
                result = pipeline.run('live', include_catalog=True, search_web=False, retry_pending=True, max_results=1)
                self.assertEqual([i['id'] for i in result['items']], ['21'])
                self.assertEqual(result['skipped'], 21)
                self.assertEqual(pipeline.db.source_item('21')['status'], 'needs_extraction')
                self.assertEqual(json.loads(pipeline.db.source_item('21')['payload_json'])['attempt_count'], 1)
            finally:
                pipeline.close()

    def test_scheduling_diversifies_hosts_and_prioritizes_existing_evidence(self):
        rows = [{'case_id': str(n), 'source_url': f'https://vendor.example/{n}', 'source_class': 'official'} for n in range(7)]
        rows += [{'case_id': 'author', 'source_url': 'https://author.example/post', 'source_class': 'practitioner'},
                 {'case_id': 'review', 'source_url': 'https://review.example/story', 'status': 'needs_scoring', 'raw_document_id': 'doc'}]
        first = prioritize_candidates(rows)[:3]
        self.assertEqual({i['case_id'] for i in first}, {'0', 'author', 'review'})
        self.assertEqual(first[0]['case_id'], 'review')

    def test_recovery_budget_is_once_per_version_and_respects_cooldown(self):
        item = {'status': 'needs_extraction', 'attempt_count': 3,
                'extraction': {'extraction_status': 'fetch_failed', 'reason': 'Source exceeds the 2 MB fetch limit'}}
        self.assertTrue(reopen_fixed_failure(item))
        self.assertEqual(item['attempt_history'][0]['attempt_count'], 3)
        item['attempt_count'] = 3
        self.assertFalse(reopen_fixed_failure(item))
        self.assertFalse(retry_ready(item))
        item.update(attempt_count=1, next_retry_at='2999-01-01T00:00:00+00:00')
        self.assertFalse(retry_ready(item))
        item['next_retry_at'] = '2000-01-01T00:00:00+00:00'
        self.assertTrue(retry_ready(item))
        blocked = {'status': 'needs_extraction', 'attempt_count': 3,
                   'extraction': {'extraction_status': 'fetch_failed', 'reason': 'HTTP 403'}}
        # A blocked platform may now use a public representation fallback;
        # the bounded retry is allowed once for this new source strategy.
        self.assertTrue(reopen_fixed_failure(blocked))

    def test_transient_error_retries_once_but_auth_error_does_not(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test'}):
            model = OpenAICompatibleModel()
        model._post_once = Mock(side_effect=[urllib.error.HTTPError('url', 503, 'busy', {}, None), {'ok': True}])
        with patch('backend.app.agent_model.time.sleep'):
            self.assertEqual(model._post('responses', {}), {'ok': True})
        self.assertEqual(model._post_once.call_count, 2)
        model._post_once = Mock(side_effect=urllib.error.HTTPError('url', 401, 'denied', {}, None))
        with self.assertRaises(urllib.error.HTTPError):
            model._post('responses', {})
        self.assertEqual(model._post_once.call_count, 1)

    def test_fenced_json_allowed_but_prose_or_incomplete_output_rejected(self):
        data = {'status': 'completed', 'output_text': '```json\n{"ok":true}\n```'}
        self.assertEqual(OpenAICompatibleModel._parse_json_result('responses', data), {'ok': True})
        for text in ('Here is JSON: {"ok":true}', '```json\n{"ok":true}'):
            with self.assertRaises(RuntimeError):
                OpenAICompatibleModel._parse_json_result('responses', {**data, 'output_text': text})

    def test_diagnostic_counts_do_not_call_technical_pending_rejection(self):
        entries = [{'status': 'needs_extraction', 'diagnostic': {'code': 'access_blocked'}},
                   {'status': 'candidate', 'diagnostic': {'code': 'below_threshold'}},
                   {'status': 'rejected', 'diagnostic': {'code': 'content_rejected'}}]
        result = summarize_outcomes(entries)
        self.assertEqual(result['technical_pending'], 1)
        self.assertEqual(result['content_rejected'], 1)
        self.assertEqual(result['below_threshold'], 1)
        self.assertEqual(result['selected'], 0)

    def test_article_with_large_script_still_yields_bounded_evidence(self):
        response = Mock()
        response.headers.get_content_type.return_value = 'text/html'
        response.read.return_value = b'<html><script>' + b'x' * 2_100_000 + b'</script><article>' + b'Actual article sentence. ' * 20 + b'</article></html>'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch('backend.app.discovery_agent.validate_public_url'), patch('backend.app.discovery_agent.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            page = SourceDiscoveryAgent(ROOT).fetch_page('https://example.com/article')
        self.assertTrue(page['text'].startswith('Actual article sentence.'))
        self.assertLess(len(page['text']), 20000)

    def test_fetch_profiles_choose_safe_public_fallbacks_by_site_type(self):
        reddit = SourceDiscoveryAgent.fetch_profile('https://www.reddit.com/r/ChatGPT/comments/abc/story')
        self.assertEqual(reddit['category'], 'community_platform')
        self.assertTrue(reddit['alternatives'][0].startswith('https://old.reddit.com/'))
        self.assertTrue(reddit['alternatives'][1].endswith('/story.json'))
        self.assertEqual(SourceDiscoveryAgent.fetch_profile('https://writer.substack.com/p/story')['category'], 'newsletter_platform')
        self.assertEqual(SourceDiscoveryAgent.fetch_profile('https://example.com/workflow.pdf')['category'], 'pdf_document')

    def test_fetch_fallback_records_strategy_and_preserves_original_citation(self):
        agent = SourceDiscoveryAgent(ROOT)
        response = Mock()
        response.headers.get_content_type.return_value = 'text/plain'
        response.read.return_value = b'An actual article with enough evidence. ' * 8
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(agent, '_fetch_bytes', side_effect=[urllib.error.HTTPError('url', 403, 'blocked', {}, None),
                                                               (response.read.return_value, 'text/plain')]):
            page = agent.fetch_page('https://writer.substack.com/p/story')
        self.assertEqual(page['url'], 'https://writer.substack.com/p/story')
        self.assertEqual(page['fetch_strategy'], 'newsletter_platform')
        self.assertIn('r.jina.ai', page['fetched_url'])

    def test_document_extractor_supports_reddit_json_and_pdf_text(self):
        text, title = SourceDiscoveryAgent._extract_document(
            b'{"title":"A real workflow","selftext":"I used AI and produced a report."}',
            'application/json', 'https://www.reddit.com/r/x/comments/y.json')
        self.assertIn('produced a report', text)
        self.assertEqual(title, 'A real workflow')


if __name__ == '__main__':
    unittest.main()
