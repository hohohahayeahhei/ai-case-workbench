import copy
import json
import tempfile
import threading
import unittest
from contextlib import closing
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app.agent_model import OpenAICompatibleModel
from backend.app.publication_dates import today_local
from backend.app.collection_pipeline import CollectionPipeline
from backend.tests.quality_fixtures import assessment, accepted_review

ROOT = Path(__file__).resolve().parents[2]


class DailyCollectionRegressions(unittest.TestCase):
    def test_connection_reset_is_retried_once(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test'}):
            model = OpenAICompatibleModel()
        for error in (ConnectionResetError('connection reset'), BrokenPipeError('closed')):
            model._post_once = Mock(side_effect=[error, {'ok': True}])
            with patch('backend.app.agent_model.time.sleep'):
                self.assertEqual(model._post('responses', {}), {'ok': True})
            self.assertEqual(model._post_once.call_count, 2)

    def test_invalid_json_is_regenerated_but_incomplete_output_is_not_accepted(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test', 'AI_CASE_LLM_WIRE_API': 'responses'}):
            model = OpenAICompatibleModel()
            model._post = Mock(side_effect=[{'status': 'completed', 'output_text': 'invalid'},
                                            {'status': 'completed', 'output_text': '{"decision":"pass"}'}])
            self.assertEqual(model.structured('verification', 'Return JSON', {})['decision'], 'pass')
            self.assertEqual(model._post.call_count, 2)
            self.assertEqual(model._post.call_args.args[1]['text']['format']['type'], 'json_object')
            model._post = Mock(return_value={'status': 'incomplete', 'output_text': '{"decision":"pass"}'})
            with self.assertRaises(RuntimeError):
                model.structured('verification', 'Return JSON', {})
            self.assertEqual(model._post.call_count, 1)
            model._post = Mock(return_value={'status': 'completed', 'output_text': 'invalid'})
            with self.assertRaises(RuntimeError):
                model.structured('verification', 'Return JSON', {})
            self.assertEqual(model._post.call_count, 2)

    def run_claim_repair(self, second_review_fails=False, verification_fails=False):
        case = {'problem': {'claim': 'Staff read tickets', 'evidence': 'Staff manually read tickets.'},
                'approach': {'claim': 'AI summarized tickets', 'evidence': 'AI summarized tickets for a human reviewer.'},
                'outcome': {'claim': 'A report was delivered', 'evidence': 'The reviewer delivered a report.'}}
        text = ' '.join(block['evidence'] for block in case.values())
        calls = []
        model = Mock()
        model.name = 'regression-test-model'
        def structured(role, system, payload):
            calls.append((role, copy.deepcopy(payload)))
            if role == 'extraction':
                result = copy.deepcopy(case)
                if sum(r == 'extraction' for r, _ in calls) == 1:
                    result['outcome']['claim'] = 'The report was independently proven accurate'
                return result
            if role == 'verification':
                repaired = sum(r == 'extraction' for r, _ in calls) > 1
                return {'decision': 'reject' if repaired and verification_fails else 'pass', 'reason': 'checked claims'}
            if role == 'scoring':
                return assessment(case)
            if role == 'quality_review':
                result = accepted_review()
                if second_review_fails or sum(r == 'quality_review' for r, _ in calls) == 1:
                    result.update(decision='revise', issues=['Do not claim independent accuracy verification.'])
                    result['editorial_checks']['factual'] = False
                return result
            raise AssertionError(role)
        model.structured.side_effect = structured
        with tempfile.TemporaryDirectory() as directory, closing(CollectionPipeline(ROOT, Path(directory) / 'catalog.db', model=model)) as pipeline:
            pipeline.live_source.call = Mock(side_effect=lambda tool, **kwargs: (
                [{'url': 'https://example.com/story', 'title': 'My workflow'}] if tool == 'search_web'
                else {'text': text, 'title': 'My workflow', 'content_hash': 'hash', 'published_at': today_local().isoformat(), 'published_at_source':'html_meta:article:published_time'}))
            result = pipeline.run('live', max_results=1)
            item = json.loads(pipeline.db.source_items()[0]['payload_json'])
            self.assertEqual(result['processed_count'], 1)
            self.assertEqual(len(set(e['event_id'] for e in result['events'])), len(result['events']))
            self.assertEqual(len([c for c in pipeline.live_source.call.call_args_list if c.args[0] == 'fetch_page']), 1)
        return result, item, calls

    def test_claim_repair_reextracts_reverifies_and_keeps_audit_history(self):
        result, item, calls = self.run_claim_repair()
        self.assertEqual([r for r, _ in calls], ['extraction', 'verification', 'scoring', 'quality_review'] * 2)
        self.assertEqual(result['counts'], {'selected': 1})
        self.assertIn('review_feedback', calls[4][1])
        self.assertEqual(item['outcome']['claim'], 'A report was delivered')
        self.assertIn('independently', item['claim_repair_history'][0]['previous_claims']['outcome']['claim'])

    def test_claim_repair_is_bounded_and_never_reuses_stale_approval(self):
        result, item, calls = self.run_claim_repair(second_review_fails=True)
        self.assertEqual(result['counts'], {'needs_scoring': 1})
        self.assertEqual(len(calls), 8)
        self.assertIsNone(item['scorecard']['quality_score'])
        result, item, calls = self.run_claim_repair(verification_fails=True)
        self.assertEqual(result['counts'], {'rejected': 1})
        self.assertEqual(len(calls), 6)
        self.assertNotIn('quality_evaluation', item)

    def test_simultaneous_starts_reuse_one_job(self):
        from backend.app import api
        with tempfile.TemporaryDirectory() as directory:
            pipeline = CollectionPipeline(ROOT, Path(directory) / 'catalog.db')
            workers = Mock()
            workers.submit.return_value = Future()
            barrier = threading.Barrier(8)
            def submit():
                barrier.wait(timeout=5)
                return api.start_collection_job(api.CollectionRunRequest(force=True))['run_id']
            try:
                with patch.object(api, 'collection_pipeline', pipeline), patch.object(api, 'collection_workers', workers), patch.object(api, 'collection_futures', {}):
                    with ThreadPoolExecutor(max_workers=8) as clients:
                        ids = list(clients.map(lambda _: submit(), range(8)))
                    self.assertEqual(len(set(ids)), 1)
                    self.assertEqual(workers.submit.call_count, 1)
                    row = pipeline.db.collection_run(ids[0])
                    row['started_at'] = '2000-01-01T00:00:00+00:00'
                    pipeline.db.save_collection_run(row)
                    self.assertEqual(api._active_live_collection()['status'], 'queued')
            finally:
                pipeline.close()


if __name__ == '__main__':
    unittest.main()
