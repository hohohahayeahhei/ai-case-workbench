"""The MCP surface must truthfully expose local state without performing a crawl."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import mcp_live_server
from backend.app.douyin_source import DouyinStore


class DouyinMCPTests(unittest.TestCase):
    def test_search_returns_browser_plan_without_creating_database_or_task(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'AI_CASE_DATA_DIR': directory}):
            result = mcp_live_server.douyin_search(query='AI 工作实战', max_results=3)
            self.assertFalse(result['executed'])
            self.assertTrue(result['read_only'])
            self.assertEqual(result['search_status'], 'browser_required')
            self.assertIn('/search/AI%20', result['browser_url'])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_fetch_cannot_treat_a_video_title_as_article_evidence(self):
        with patch.object(mcp_live_server, 'SourceDiscoveryAgent') as discovery:
            result = mcp_live_server.fetch_page('https://www.douyin.com/video/7610000000000000001')
        self.assertEqual(result['source_status'], 'awaiting_browser')
        self.assertEqual(result['fetch_status'], 'failed')
        discovery.assert_not_called()
        self.assertNotIn('text', result)

    def test_local_record_reads_do_not_mutate_the_database(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'AI_CASE_DATA_DIR': directory}):
            path = Path(directory) / 'catalog.db'
            store = DouyinStore(path)
            record = store.import_link('https://www.douyin.com/video/7610000000000000001')
            store.set_status(record['item_id'], 'waiting_login', reason='受控测试登录状态')
            store.close()
            before = path.read_bytes()
            result = mcp_live_server.douyin_read(record['item_id'])
            self.assertTrue(result['found'])
            self.assertEqual(result['item']['status'], 'waiting_login')
            self.assertEqual(mcp_live_server.douyin_status()['tasks'], [])
            self.assertEqual(path.read_bytes(), before)

    def test_author_plan_rejects_credentials_private_and_deceptive_hosts(self):
        for value in ('http://127.0.0.1/user/a', 'https://douyin.com.evil.example/user/a',
                      'https://me:password@www.douyin.com/user/a', 'https://www.douyin.com:444/user/a'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mcp_live_server.douyin_search(author_url=value)


if __name__ == '__main__':
    unittest.main()
