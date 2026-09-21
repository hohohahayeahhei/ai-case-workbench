import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.api import app
from backend.app.query_agent import QueryAgent


ROOT = Path(__file__).resolve().parents[2]


class QueryAgentTests(unittest.TestCase):
    def setUp(self):
        self.agent = QueryAgent(ROOT)
        self.client = TestClient(app)

    def test_query_returns_stable_read_only_contract(self):
        result = self.agent.query(window="all", source_mode="online_snapshot", limit=2)
        self.assertEqual(result["contract_version"], "v1")
        self.assertTrue(result["meta"]["read_only"])
        self.assertEqual(len(result["items"]), 2)
        self.assertIn("evidence", result["items"][0])

    def test_query_filters_topic_and_excludes_bad_fixture_records(self):
        result = self.agent.query(query="客服", window="all", source_mode="all")
        self.assertTrue(result["items"])
        self.assertTrue(all("300%" not in item["title"] for item in result["items"]))

    def test_snapshot_cursor_pages_without_duplicates(self):
        first = self.agent.snapshot(limit=1)
        self.assertIsNotNone(first["next_cursor"])
        second = self.agent.snapshot(cursor=first["next_cursor"], limit=10)
        ids = [item["id"] for item in first["items"] + second["items"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_http_query_contract(self):
        response = self.client.get("/api/v1/cases", params={"window": "all", "limit": 2})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["contract_version"], "v1")
        self.assertTrue(payload["meta"]["read_only"])

    def test_invalid_cursor_is_a_client_error(self):
        response = self.client.get("/api/v1/cases", params={"cursor": "bad cursor"})
        self.assertEqual(response.status_code, 400)

    def test_selected_snapshot_separates_page_and_sync_cursor(self):
        response = self.client.get("/api/v1/selected/snapshot", params={"limit": 1})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("cursor", payload)
        self.assertIn("page", payload)
        changes = self.client.get("/api/v1/selected/changes", params={"cursor": payload["cursor"]})
        self.assertEqual(changes.status_code, 200)
        self.assertEqual(changes.json()["changes"], [])


if __name__ == "__main__":
    unittest.main()
