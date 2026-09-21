"""Capacity contracts: admission cannot outrun the bounded processing batch."""
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from backend.app.agent_model import MockAgentModel
from backend.app.collection_pipeline import CollectionPipeline
from backend.app.collection_health import PROCESSING_VERSION

ROOT = Path(__file__).resolve().parents[2]


class CollectionCapacityTests(unittest.TestCase):
    def pipeline(self, directory):
        pipeline = CollectionPipeline(ROOT, Path(directory) / 'catalog.db', model=MockAgentModel())
        pipeline.discovery.search_branches = Mock(return_value=[
            {'id': str(i), 'label': str(i), 'query': str(i)} for i in range(4)])
        pipeline.discovery.catalog_feed_urls = Mock(return_value=['https://feed.example/a', 'https://feed.example/b'])
        counter = 0
        def search(tool, **kwargs):
            nonlocal counter
            self.assertEqual(tool, 'search_web')
            counter += 1
            # Deliberately violate the provider limit; admission must still cap it.
            return [{'url': f'https://web.example/{counter}/{i}'} for i in range(100)]
        pipeline.live_source.call = Mock(side_effect=search)
        pipeline.discovery.discover_feeds = Mock(side_effect=lambda urls, **kwargs: [
            {'case_id': f'{urls[0]}-{i}', 'source_url': f'{urls[0]}/{i}',
             'discovery_mode': 'rss', 'status': 'needs_extraction'} for i in range(100)])
        pipeline._enrich_feed_item = Mock(side_effect=lambda item, **kwargs: item)
        def process(item):
            # A resolved terminal outcome without invoking a model/network.
            item['status'] = 'rejected'
            pipeline.db.update_source_item(item['case_id'], 'rejected', item)
            return {'id': item['case_id'], 'status': 'rejected'}
        pipeline._process = Mock(side_effect=process)
        return pipeline

    def seed(self, pipeline, count, **extra):
        for i in range(count):
            item = {'case_id': f'old-{i}', 'source_url': f'https://old.example/{i}',
                    'discovery_mode': 'web_search', 'status': 'needs_extraction', **extra}
            pipeline.db.save_source_item(item, 'needs_extraction')

    def test_fresh_web_and_rss_share_one_limit_even_if_adapters_over_return(self):
        with tempfile.TemporaryDirectory() as directory, closing(self.pipeline(directory)) as pipeline:
            result = pipeline.run('live', max_results=12, include_catalog=True)
            self.assertEqual(result['new_candidates'], 12)
            self.assertEqual(result['discovered'], 12)
            self.assertEqual(result['processed_count'], 12)
            self.assertEqual(result['queued'], 0)
            requests = sum(c.kwargs['max_results'] for c in pipeline.live_source.call.call_args_list)
            requests += sum(c.kwargs['max_results'] for c in pipeline.discovery.discover_feeds.call_args_list)
            self.assertEqual(requests, 12)
            self.assertEqual(len(pipeline.db.source_items()), 12)
            self.assertEqual(pipeline.discovery.discover_feeds.call_count, 2)

    def test_eight_existing_reserve_eight_and_only_search_four(self):
        with tempfile.TemporaryDirectory() as directory, closing(self.pipeline(directory)) as pipeline:
            self.seed(pipeline, 8)
            # Omitting retry_pending must not grow backlog instead of consuming it.
            result = pipeline.run('live', max_results=12, include_catalog=True)
            self.assertEqual(result['carried_over'], 8)
            self.assertEqual(result['discovery_budget'], 4)
            self.assertEqual(result['discovery_requested'], 4)
            self.assertEqual(result['new_candidates'], 4)
            self.assertEqual(result['processed_count'], 12)
            self.assertEqual(result['discovered'], 4)
            self.assertEqual(len(pipeline.db.source_items()), 12)

    def test_large_backlog_never_searches_and_history_is_not_new_discovery(self):
        with tempfile.TemporaryDirectory() as directory, closing(self.pipeline(directory)) as pipeline:
            self.seed(pipeline, 125)
            result = pipeline.run('live', max_results=12, include_catalog=True,
                                  feed_urls=['https://explicit.example/feed'])
            self.assertEqual(result['discovered'], 0)
            self.assertEqual(result['carried_over'], 12)
            self.assertEqual(result['ready_at_start'], 125)
            self.assertEqual(result['queued'], 113)
            self.assertEqual(result['processed_count'], 12)
            self.assertEqual(len(result['scheduled']), 12)
            pipeline.live_source.call.assert_not_called()
            pipeline.discovery.discover_feeds.assert_not_called()
            pipeline.discovery.catalog_feed_urls.assert_not_called()
            self.assertEqual(pipeline.db.connection.execute('SELECT COUNT(*) FROM source_items').fetchone()[0], 125)

    def test_duplicates_consume_search_allowance_without_refill_or_reprocessing(self):
        with tempfile.TemporaryDirectory() as directory, closing(self.pipeline(directory)) as pipeline:
            pipeline.db.save_source_item({'case_id': 'done', 'source_url': 'https://done.example/story',
                                         'discovery_mode': 'web_search'}, 'rejected')
            pipeline.live_source.call = Mock(return_value=[{'url': 'https://done.example/story?utm_source=x'}] * 20)
            result = pipeline.run('live', max_results=3, include_catalog=True)
            self.assertEqual(result['discovery_requested'], 3)
            self.assertEqual(result['new_candidates'], 0)
            self.assertEqual(result['processed_count'], 0)
            self.assertEqual(result['skipped'], 3)
            self.assertEqual(len(pipeline.db.source_items()), 1)

    def test_cooling_records_do_not_consume_available_processing_slots(self):
        with tempfile.TemporaryDirectory() as directory, closing(self.pipeline(directory)) as pipeline:
            self.seed(pipeline, 8, processing_version=PROCESSING_VERSION, attempt_count=1,
                      next_retry_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
            result = pipeline.run('live', max_results=3)
            self.assertEqual(result['backlog_at_start'], 8)
            self.assertEqual(result['ready_at_start'], 0)
            self.assertEqual(result['new_candidates'], 3)
            self.assertEqual(result['processed_count'], 3)
            self.assertEqual(len(pipeline.db.source_items()), 11)

    def test_retry_only_does_not_backfill_unused_slots_with_new_sources(self):
        with tempfile.TemporaryDirectory() as directory, closing(self.pipeline(directory)) as pipeline:
            self.seed(pipeline, 2)
            result = pipeline.run('live', max_results=12, search_web=False,
                                  retry_pending=True, include_catalog=True)
            self.assertEqual(result['processed_count'], 2)
            self.assertEqual(result['new_candidates'], 0)
            pipeline.live_source.call.assert_not_called()
            pipeline.discovery.discover_feeds.assert_not_called()


if __name__ == '__main__':
    unittest.main()
