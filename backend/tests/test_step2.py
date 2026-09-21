import tempfile
import unittest
from pathlib import Path

from backend.app.demo_pipeline import run_demo
from backend.app.db import Database, normalize_url
from backend.app.mock_mcp import MockMCPRegistry


class StepTwoTests(unittest.TestCase):
    def test_normalize_url_removes_tracking_parameters(self):
        self.assertEqual(
            normalize_url("https://example.com/a/?utm_source=feed#part"),
            "https://example.com/a",
        )

    def test_registry_exposes_four_mcp_servers(self):
        db = Database()
        db.init_schema()
        registry = MockMCPRegistry(Path("data/fixtures/cases.json"), db)
        self.assertEqual(set(registry.catalog()), {"source", "evidence", "knowledge_base", "distribution"})
        self.assertIn("search_sources", {tool.name for tool in registry.catalog()["source"]})

    def test_happy_path_verifies_one_case_and_rejects_two(self):
        result = run_demo()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["counts"]["verified"], 1)
        self.assertEqual(result["counts"]["rejected"], 2)

    def test_conflict_blocks_publish(self):
        result = run_demo("sync_conflict")
        self.assertEqual(result["status"], "blocked")

    def test_unknown_receipt_needs_attention(self):
        result = run_demo("publish_unknown")
        self.assertEqual(result["status"], "needs_attention")


if __name__ == "__main__":
    unittest.main()
