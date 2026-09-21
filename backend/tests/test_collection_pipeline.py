import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.api import app, collection_pipeline
from backend.app.collection_pipeline import CollectionPipeline


ROOT = Path(__file__).resolve().parents[2]


class CollectionPipelineTests(unittest.TestCase):
    def test_offline_collection_preserves_snapshots_without_fabricating_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = CollectionPipeline(ROOT, Path(directory) / "catalog.db")
            try:
                result = pipeline.run("online_snapshot")
                self.assertEqual(result["discovered"], 3)
                self.assertEqual(result["counts"].get("needs_scoring"), 3)
                self.assertFalse(pipeline.db.source_items(["selected"]))
            finally:
                pipeline.close()

    def test_feed_candidate_stays_out_of_selected_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = CollectionPipeline(ROOT, Path(directory) / "catalog.db")
            try:
                item = {
                    "case_id": "feed-1", "title_original": "AI workflow", "source_url": "https://example.com/a",
                    "source_type": "rss", "status": "needs_extraction",
                }
                result = pipeline._process(item)
                self.assertEqual(result["status"], "needs_extraction")
            finally:
                pipeline.close()

    def test_model_outage_keeps_fetched_feed_candidate_pending(self):
        class OfflineModel:
            def structured(self, role, system, payload):
                raise RuntimeError("model unavailable")

        with tempfile.TemporaryDirectory() as directory:
            pipeline = CollectionPipeline(ROOT, Path(directory) / "catalog.db", model=OfflineModel())
            pipeline.discovery.discover_feeds = lambda urls, query="", max_results=100: [{
                "case_id": "feed-outage-1",
                "title_original": "客服自动化",
                "source_url": "https://example.com/article",
                "source_type": "rss",
                "status": "needs_extraction",
            }]
            pipeline.discovery.fetch_page = lambda url: {
                "url": url,
                "domain": "example.com",
                "title": "客服自动化",
                "text": "团队使用 Claude 自动总结客服记录。",
                "content_hash": "hash-2",
            }
            try:
                result = pipeline.run("online_snapshot", feed_urls=["https://example.com/feed.xml"])
                self.assertEqual(result["counts"].get("needs_extraction"), 1)
                row = pipeline.db.source_items(["needs_extraction"])[0]
                self.assertEqual(row["source_item_id"], "feed-outage-1")
            finally:
                pipeline.close()

    def test_feed_article_without_valid_scoring_contract_stays_pending(self):
        class SafeModel:
            def structured(self, role, system, payload):
                if role == "verification":
                    return {"decision": "pass", "reason": "Source supports claims"}
                return {
                    "problem": {"claim": "客户问题需要人工处理", "evidence": "客户问题需要人工处理"},
                    "approach": {"claim": "团队使用 Claude 自动总结", "evidence": "团队使用 Claude 自动总结"},
                    "outcome": {"claim": "处理时间减少 30 分钟", "evidence": "处理时间减少 30 分钟"},
                    "limitations": [],
                }

        with tempfile.TemporaryDirectory() as directory:
            pipeline = CollectionPipeline(ROOT, Path(directory) / "catalog.db", model=SafeModel())
            pipeline.discovery.discover_feeds = lambda urls, query="", max_results=100: [{
                "case_id": "feed-model-1",
                "title_original": "客服自动化",
                "source_url": "https://example.com/article",
                "source_type": "rss",
                "status": "needs_extraction",
            }]
            pipeline.discovery.fetch_page = lambda url: {
                "url": url,
                "domain": "example.com",
                "title": "客服自动化",
                "text": "客户问题需要人工处理。团队使用 Claude 自动总结。处理时间减少 30 分钟。",
                "content_hash": "hash-1",
            }
            try:
                result = pipeline.run(
                    "online_snapshot",
                    feed_urls=["https://example.com/feed.xml"],
                )
                self.assertEqual(result["counts"].get("needs_scoring"), 4)
                candidate = pipeline.db.source_items(["needs_scoring"])
                self.assertIn("feed-model-1", {row["source_item_id"] for row in candidate})
                payload = next(row for row in candidate if row["source_item_id"] == "feed-model-1")["payload_json"]
                self.assertIn("dimensions 必须恰好包含六个评分维度", payload)
                self.assertNotIn("unsupported_metric", payload)
            finally:
                pipeline.close()

    def test_source_catalog_exposes_public_feeds(self):
        agent = CollectionPipeline(ROOT, ":memory:").discovery
        self.assertEqual(
            agent.catalog_feed_urls(),
            [
                "https://github.blog/feed/",
                "https://huggingface.co/blog/feed.xml",
                "https://www.microsoft.com/en-us/research/feed/",
            ],
        )

    def test_catalog_feed_failure_is_recorded_and_pipeline_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = CollectionPipeline(ROOT, Path(directory) / "catalog.db")
            try:
                pipeline.discovery.catalog_feed_urls = lambda: ["https://invalid.example/feed.xml"]
                def failed(*args, **kwargs):
                    raise OSError("simulated source outage")
                pipeline.discovery.discover_feeds = failed
                result = pipeline.run("online_snapshot", include_catalog=True)
                self.assertEqual(result["discovered"], 3)
                self.assertEqual(len(result["feed_errors"]), 1)
            finally:
                pipeline.close()

    def test_collection_api_exposes_catalog_and_run(self):
        client = TestClient(app)
        self.assertEqual(client.get("/api/v1/sources").status_code, 200)
        response = client.post("/api/v1/collection/runs", json={"source_mode": "online_snapshot"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["discovered"], 3)
        self.assertTrue(client.get("/api/v1/collection/items").json()["items"])


if __name__ == "__main__":
    unittest.main()
