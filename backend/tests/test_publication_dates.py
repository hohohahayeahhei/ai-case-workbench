import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app.publication_dates import (parse_publication_date, publication_metadata, date_status,
                                          date_policy, recent_cutoff, today_local, temporal_view)
from backend.app.discovery_agent import SourceDiscoveryAgent
from backend.app.collection_pipeline import CollectionPipeline
from backend.app.query_agent import QueryAgent
from backend.app.collection_export import live_records, tencent_rows
from backend.tests.quality_fixtures import approve

ROOT = Path(__file__).resolve().parents[2]


class PublicationDateTests(unittest.TestCase):
    def test_calendar_year_boundaries_and_future_dates(self):
        today = date(2026, 9, 16)
        self.assertEqual(recent_cutoff(today), date(2025, 9, 16))
        self.assertEqual(recent_cutoff(date(2024, 2, 29)), date(2023, 2, 28))
        self.assertEqual(date_status('2025-09-16', today), 'recent')
        self.assertEqual(date_status('2025-09-15', today), 'outdated')
        self.assertEqual(date_status('2026-09-16', today), 'recent')
        self.assertEqual(date_status('2026-09-17', today), 'future')

    def test_common_publisher_formats_and_missing_precision(self):
        for text in ('Fri, 11 Sep 2026 18:26:10 +0000', '2026-09-11T18:26:10Z', 'September 11, 2026', '2026年9月11日', '2026/09/11'):
            self.assertEqual(parse_publication_date(text), date(2026, 9, 11), text)
        for text in ('2026', '2026-09', '2026-02-30', 'yesterday', None):
            self.assertIsNone(parse_publication_date(text))

    def test_html_metadata_and_modification_are_separate(self):
        html = '<meta content="2024-01-20" property="article:published_time"><meta property="article:modified_time" content="2026-09-16">'
        result = publication_metadata(html)
        self.assertEqual(result['published_at'], '2024-01-20')
        self.assertEqual(result['updated_at'], '2026-09-16')
        self.assertEqual(result['published_at_source'], 'html_meta:article:published_time')

    def test_json_ld_ignores_related_articles_and_creation_date(self):
        graph = {'@graph': [{'@type': 'BlogPosting', 'url': 'https://example.com/related', 'datePublished': '2026-09-10'},
                            {'@type': 'BlogPosting', 'url': 'https://example.com/story', 'datePublished': '2025-10-01', 'dateModified': '2026-09-10'}]}
        result = publication_metadata('<script type="application/ld+json">'+json.dumps(graph)+'</script>', source_url='https://example.com/story')
        self.assertEqual(result['published_at'], '2025-10-01')
        self.assertNotIn('published_at', publication_metadata('<script type="application/ld+json">{"@type":"Organization","dateCreated":"2026-09-10"}</script>'))

    def test_update_copyright_and_generic_time_do_not_become_publication(self):
        self.assertNotIn('published_at', publication_metadata('<footer>Copyright 2026</footer><time datetime="2026-09-16">Today</time><meta itemprop="dateModified" content="2026-09-16">'))
        self.assertEqual(publication_metadata('Published Time: 2026-09-15T10:00:00Z\nAn article', 'text/plain')['published_at'], '2026-09-15')

    def test_atom_updated_is_not_used_as_published(self):
        items = SourceDiscoveryAgent._parse_feed(b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Story</title><link href="https://example.com/story"/><updated>2026-09-15</updated></entry></feed>', 'https://example.com/feed')
        self.assertFalse(items[0]['published_at'])
        self.assertEqual(items[0]['updated_at'], '2026-09-15')

    def test_collection_time_cannot_make_an_old_article_recent(self):
        item = {'discovery_mode': 'web_search', 'status': 'selected', 'published_at': '2000-01-01', 'collected_at': today_local().isoformat()}
        self.assertEqual(temporal_view(item)['status'], 'outdated')
        self.assertFalse(QueryAgent._within_window(item, '1y'))
        self.assertFalse(QueryAgent._within_window(item, '7d'))
        item['published_at'] = None
        self.assertEqual(temporal_view(item)['status'], 'needs_date')

    def test_live_query_snapshot_and_export_enforce_dates_even_with_all_window(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = CollectionPipeline(ROOT, Path(directory)/'catalog.db', model=Mock())
            query = QueryAgent(ROOT, pipeline.db.path)
            try:
                for name, value in [('recent', today_local().isoformat()), ('old', '2000-01-01'), ('unknown', None), ('future', (today_local()+timedelta(days=1)).isoformat())]:
                    item = SourceDiscoveryAgent(ROOT).fixture_candidates()[0]
                    item.update(case_id=name, published_at=value, discovery_mode='web_search', source_url='https://example.com/'+name,
                                collected_at=today_local().isoformat(), verification={'decision':'pass', 'reason':'checked'})
                    for key in ('problem','approach','outcome'):
                        item[key]['claim'] = item[key]['evidence']
                    approve(item)
                    pipeline.db.save_source_item(item, 'selected')
                self.assertEqual([x['id'] for x in query.query(window='all', source_mode='live')['items']], ['recent'])
                self.assertEqual([x['id'] for x in query.selected_snapshot(source_mode='live')['items']], ['recent'])
                self.assertEqual([x['case_id'] for x in live_records(pipeline.db)], ['recent'])
                self.assertEqual(tencent_rows(pipeline.db)[0]['原文发布日期'], today_local().isoformat())
            finally:
                query.catalog_db.close(); pipeline.close()

    def test_date_gate_preserves_raw_document_and_does_not_call_model(self):
        for value, expected in [(None, 'needs_date'), ('2000-01-01', 'outdated')]:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                model = Mock()
                pipeline = CollectionPipeline(ROOT, Path(directory)/'catalog.db', model=model)
                try:
                    pipeline.live_source.call = Mock(side_effect=[
                        [{'url':'https://example.com/story', 'title':'Story', 'published_at':today_local().isoformat()}],
                        {'text':'An original article. '*30, 'content_hash':'hash', 'published_at':value}])
                    result = pipeline.run('live', max_results=1)
                    self.assertEqual(result['counts'], {expected:1})
                    self.assertEqual(len(pipeline.db.raw_documents()), 1)
                    model.structured.assert_not_called()
                    self.assertIn(date_policy()['cutoff'], pipeline.live_source.call.call_args_list[0].kwargs['query'])
                    item = json.loads(pipeline.db.source_items()[0]['payload_json'])
                    self.assertEqual(item['published_at'], value)
                finally:
                    pipeline.close()

    def test_raw_date_metadata_survives_cache_and_legacy_schema_migration(self):
        from backend.app.db import Database
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'catalog.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE raw_documents(document_id TEXT PRIMARY KEY, source_item_id TEXT, source_url TEXT, content_hash TEXT, title TEXT, body TEXT, fetched_at TEXT, fetch_status TEXT)')
            db = Database(path)
            try:
                db.init_schema(); db.init_schema()
                db.save_raw_document({'document_id':'doc-a', 'source_item_id':'a', 'source_url':'https://example.com/a', 'content_hash':'hash', 'text':'article', 'published_at':'2026-09-15', 'published_at_source':'html_meta:article:published_time'})
                metadata = json.loads(db.raw_documents()[0]['metadata_json'])
                self.assertEqual(metadata['published_at'], '2026-09-15')
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
