import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app.agent_model import OpenAICompatibleModel, MockAgentModel
from backend.app.collection_pipeline import CollectionPipeline
from backend.app.collection_export import export_collection
from backend.app.db import normalize_url
from backend.app.extraction_agent import ExtractionAgent
from backend.app.query_agent import QueryAgent
from backend.app.scoring import score_case
from backend.tests.quality_fixtures import approve

ROOT = Path(__file__).resolve().parents[2]


class LiveCollectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'catalog.db'
        self.pipeline = CollectionPipeline(ROOT, self.path, model=MockAgentModel())
        self.addCleanup(self.pipeline.close)

    def test_live_search_failure_does_not_inject_fixtures(self):
        self.pipeline.live_source.call = Mock(side_effect=RuntimeError('search unavailable'))
        result = self.pipeline.run('live')
        self.assertEqual(result['discovered'], 0)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(self.pipeline.db.sync_state('collection')['complete'])
        self.assertEqual(self.pipeline.db.collection_run(result['run_id'])['status'], 'failed')

    def test_mock_extraction_remains_pending(self):
        result = ExtractionAgent().extract_case({'title_original': 'AI workflow'}, {'text': 'Example text'}, MockAgentModel())
        self.assertEqual(result['extraction_status'], 'needs_structured_claims')

    def test_true_quote_cannot_ground_a_different_number(self):
        data = {key: {'claim': '作者称节省 999 分钟', 'evidence': '作者称节省 30 分钟'} for key in ('problem','approach','outcome')}
        self.assertFalse(ExtractionAgent._valid_case_evidence(data, '作者称节省 30 分钟'))

    def test_extraction_repairs_an_unsupported_number_once(self):
        valid = {key:{'claim':'作者称节省 30 分钟','evidence':'作者称节省 30 分钟'} for key in ('problem','approach','outcome')}
        invalid = copy.deepcopy(valid)
        invalid['outcome']['claim'] = '作者称节省 999 分钟'
        model = Mock()
        model.structured.side_effect = [invalid, valid]
        result = ExtractionAgent().extract_case({}, {'text':'作者称节省 30 分钟'}, model)
        self.assertEqual(result['extraction_status'], 'structured_claims')
        self.assertEqual(result['outcome']['claim'], '作者称节省 30 分钟')
        self.assertTrue(model.structured.call_args.args[2]['validation_errors'])

    def test_limitations_cannot_turn_missing_evidence_into_points(self):
        case = self.pipeline.discovery.fixture_candidates()[0]
        case['outcome'] = {}
        case['limitations'] = ['No outcome']
        score = score_case(case)
        self.assertFalse(score['selected'])
        self.assertIn('missing_evidence', score['hard_flags'])
        self.assertIsNone(score['dimensions']['outcome_evidence'])

    def test_repeat_url_preserves_selected_record_and_does_not_call_model(self):
        item = self.pipeline.discovery.fixture_candidates()[0]
        item.update(discovery_mode='web_search', verification={'decision':'pass','reason':'source checked'})
        self.pipeline.db.save_source_item(item, 'selected')
        self.pipeline.live_source.call = Mock(return_value=[{'url': item['source_url']+'?utm_source=repeat', 'title': 'Changed'}])
        result = self.pipeline.run('live', max_results=1)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(self.pipeline.live_source.call.call_count, 1)
        self.assertEqual(self.pipeline.db.source_item(item['case_id'])['status'], 'selected')

    def test_malformed_feed_is_isolated(self):
        import xml.etree.ElementTree as ET
        self.pipeline.discovery.discover_feeds = Mock(side_effect=ET.ParseError('invalid XML'))
        result = self.pipeline.run('live', feed_urls=['https://example.com/feed'], search_web=False)
        self.assertEqual(result['status'], 'failed')

    def test_fixture_record_cannot_bypass_live_verification(self):
        item = self.pipeline.discovery.fixture_candidates()[0]
        self.pipeline.db.save_source_item(item, 'selected')
        self.pipeline.live_source.call = Mock(side_effect=[
            [{'url':item['source_url'],'title':item['title_original']}], RuntimeError('source unavailable')])
        result = self.pipeline.run('live', max_results=1)
        self.assertEqual(result['skipped'], 0)
        self.assertEqual(result['counts'], {'needs_extraction':1})
        self.assertEqual(self.pipeline.live_source.call.call_count, 2)

    def test_semantically_unsupported_case_is_not_selected(self):
        item = {'case_id':'semantic', 'source_url':'https://example.com/story', 'title_original':'AI workflow', 'status':'needs_extraction'}
        text = 'Staff manually read tickets before. The team used AI to summarize tickets. They completed the work in one afternoon.'
        claims = dict(zip(('problem','approach','outcome'), [{'claim': s, 'evidence': s} for s in text.split('. ') ]))
        self.pipeline.model = Mock()
        self.pipeline.model.structured.side_effect = [claims, {'decision':'reject','reason':'Hypothetical workflow, not observed'}]
        self.pipeline.discovery.fetch_page = Mock(return_value={'text':text,'content_hash':'abc'})
        self.pipeline.db.save_source_item(item)
        enriched = self.pipeline._enrich_feed_item(item)
        result = self.pipeline._process(enriched)
        self.assertEqual(result['status'], 'rejected')

    def test_live_catalog_change_requires_snapshot_reset(self):
        query = QueryAgent(ROOT, self.path)
        self.addCleanup(query.catalog_db.close)
        first = query.selected_snapshot(source_mode='live')
        item = self.pipeline.discovery.fixture_candidates()[0]
        item.update(discovery_mode='web_search', verification={'decision':'pass','reason':'source checked'})
        self.pipeline.db.save_source_item(item, 'selected')
        for key in ("problem", "approach", "outcome"):
            item[key]["claim"] = item[key]["evidence"]
        approve(item)
        self.pipeline.db.update_source_item(item['case_id'], 'selected', item)
        self.assertTrue(query.selected_changes(first['cursor'], source_mode='live')['reset_required'])

    def test_export_is_pending_and_excludes_demo_records(self):
        result = self.pipeline.run('online_snapshot')
        paths = export_collection(self.pipeline.db, result, Path(self.directory.name)/'exports')
        manifest = json.loads(Path(paths['tencent_manifest']).read_text())
        self.assertEqual(manifest['delivery_status'], 'stored_locally')
        self.assertEqual(manifest['records'], [])

    def test_normalization_preserves_case_sensitive_paths(self):
        self.assertEqual(normalize_url('https://EXAMPLE.com/Case?utm_source=x#foo'), 'https://example.com/Case')
        self.assertNotEqual(normalize_url('https://example.com/Case'), normalize_url('https://example.com/case'))

    def test_incomplete_response_is_rejected_even_with_json_text(self):
        with self.assertRaises(RuntimeError):
            OpenAICompatibleModel._parse_json_result('responses', {'status':'incomplete','output_text':'{"ok":true}'})

    def test_gateway_sse_requires_a_completed_response(self):
        partial = 'data: {"type":"response.output_text.delta","delta":"{\\"ok\\":true}"}\n\n'
        with self.assertRaises(RuntimeError):
            OpenAICompatibleModel._parse_sse_result(partial + 'data: [DONE]\n\n')
        response = {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{"ok":true}'}]}]}
        raw = partial + 'event: response.completed\ndata: ' + json.dumps({'type':'response.completed','response':response}) + '\n\n'
        self.assertEqual(OpenAICompatibleModel._parse_json_result('responses', OpenAICompatibleModel._parse_sse_result(raw)), {'ok':True})

    def test_web_search_rejects_model_invented_urls_without_tool_execution(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY':'test'}):
            model = OpenAICompatibleModel()
        model._post = Mock(return_value={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','annotations':[{'type':'url_citation','url':'https://example.com'}]}]}]})
        with self.assertRaisesRegex(RuntimeError, 'did not execute'):
            model.search_web('AI cases')

    def test_web_search_accepts_only_cited_tool_results(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY':'test'}):
            model = OpenAICompatibleModel()
        model._post = Mock(return_value={'status':'completed','output':[
            {'type':'web_search_call','status':'completed','action':{}},
            {'type':'message','content':[{'type':'output_text','text':'Uncited https://fake.example', 'annotations':[
                {'type':'url_citation','url':'https://example.com/story','title':'Story'}]}]}]})
        self.assertEqual([r['url'] for r in model.search_web('AI cases')], ['https://example.com/story'])


if __name__ == '__main__':
    unittest.main()
