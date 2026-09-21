"""Local HTTP intake boundary tests. No live-browser success is implied."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.douyin_api import create_douyin_router
from backend.app.douyin_source import DouyinStore
from backend.tests.test_douyin_source import URL, URL2, SHORT, audio_bytes, evidence, observation


BASE = "/api/v1/douyin"


class DouyinApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = DouyinStore(Path(self.directory.name) / "catalog.db")
        self.pipeline = Mock()
        self.pipeline.db.source_item.return_value = None
        self.submit = Mock(return_value={"job_id": "controlled-review"})
        self.app = FastAPI()
        self.app.include_router(create_douyin_router(self.store, self.pipeline, self.submit))
        self.client = TestClient(self.app)
        self.item = self.client.post(BASE + "/import", json={"text": URL}).json()["item"]
        self.item_path = BASE + "/items/" + self.item["item_id"]

    def tearDown(self):
        self.client.close()
        self.store.close()
        self.directory.cleanup()

    def test_metadata_only_stays_unready_and_review_is_not_submitted(self):
        response = self.client.post(self.item_path + "/observation", json=observation())
        self.assertEqual(response.status_code, 200)
        item = response.json()["item"]
        self.assertEqual(item["status"], "media_unavailable")
        self.assertEqual(item["segments"], [])
        self.assertEqual(item["author_name"], "公开作者")
        self.assertIsNone(item["media_url"])
        review = self.client.post(self.item_path + "/review")
        self.assertEqual(review.status_code, 409)
        self.submit.assert_not_called()

    def test_malformed_origin_is_rejected_without_server_error(self):
        for origin in ('http://[broken', 'file://localhost', 'http://user@localhost:5173', 'http://localhost:bad', 'null'):
            with self.subTest(origin=origin):
                response = self.client.post(BASE + '/import', json={'text': URL}, headers={'Origin': origin})
                self.assertEqual(response.status_code, 403)

    def test_processed_review_does_not_enqueue_an_empty_collection(self):
        self.store.record_observation(self.item['item_id'], observation(evidence=[evidence(), evidence('visible_frame', 'outcome')]))
        self.store.mark_processed(self.item['item_id'], 'douyin-7612345678901234567', 'rejected')
        response = self.client.post(self.item_path + '/review')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['item']['status'], 'processed')
        self.submit.assert_not_called()

    def test_discovery_batch_pause_and_resume_keep_real_progress(self):
        task = self.client.post(BASE + '/discover', json={'query': '受控关键词', 'max_results': 1}).json()['task']
        path = BASE + '/discovery/tasks/' + task['task_id'] + '/observations'
        result = self.client.post(path, json={'status': 'waiting_login', 'reason': '页面要求扫码', 'observations': []})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(self.store.source_state()['paused'])
        self.client.post(BASE + '/source/resume')
        result = self.client.post(path, json={'status': 'completed', 'observations': [observation(canonical_url=URL)]})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['task']['progress']['item_ids'], [self.item['item_id']])

    def test_missing_and_collection_dates_cannot_pass_date_gate(self):
        response = self.client.post(self.item_path + "/observation", json={"title": "今天发布 十倍效率"})
        self.assertEqual(response.json()["item"]["status"], "date_unknown")
        self.assertIsNone(response.json()["item"]["published_at"])
        bad_date = self.client.post(self.item_path + "/observation", json=observation(published_at_source="采集日期"))
        self.assertEqual(bad_date.status_code, 400)
        self.assertEqual(self.client.post(self.item_path + "/review").status_code, 409)

    def test_posted_time_evidence_keeps_kind_role_and_uncertainty(self):
        self.client.post(self.item_path + "/observation", json=observation())
        oral = evidence("machine_transcript", "workflow", uncertainty="品牌名可能识别错误")
        oral.update(start=3, end=8)
        result = self.client.post(self.item_path + "/evidence", json={"segments": [oral]})
        self.assertEqual(result.status_code, 200)
        item = result.json()["item"]
        self.assertEqual(item["status"], "evidence_insufficient")
        segment = item["segments"][0]
        self.assertEqual(segment["start_seconds"], 3)
        self.assertEqual(segment["role"], "workflow")
        self.assertEqual(segment["uncertainty"], "品牌名可能识别错误")
        self.assertTrue(segment["machine_generated"])
        self.assertFalse(segment["verified"])

    def test_unverified_machine_frame_cannot_be_upgraded_by_truthy_false_string(self):
        self.client.post(self.item_path + "/observation", json=observation(evidence=[evidence()]))
        result = self.client.post(self.item_path + "/evidence", json={"segments": [evidence("visible_frame", "outcome", machine_generated=True, verified=False)]})
        self.assertEqual(result.json()["item"]["status"], "evidence_insufficient")
        for invalid in ("false", "true", 1, 0, None):
            with self.subTest(invalid=invalid):
                result = self.client.post(self.item_path + "/evidence", json={"segments": [evidence("visible_frame", "outcome", machine_generated=True, verified=invalid)]})
                self.assertEqual(result.status_code, 400)
        result = self.client.post(self.item_path + "/evidence", json={"segments": [evidence("visible_frame", "outcome", machine_generated=True, verified=True)]})
        self.assertEqual(result.json()["item"]["status"], "ready")
        self.assertEqual(self.client.post(self.item_path + "/review").status_code, 202)
        self.submit.assert_called_once_with("douyin-7612345678901234567")

    def test_invalid_evidence_position_and_source_fail_without_mutating_record(self):
        for invalid in (evidence(start_seconds=-1), evidence(end_seconds=1), evidence(role="success"),
                        evidence(source_url="http://127.0.0.1:8000/private"), evidence(source_url=URL2)):
            with self.subTest(invalid=invalid):
                result = self.client.post(self.item_path + "/evidence", json={"segments": [invalid]})
                self.assertEqual(result.status_code, 400)
        self.assertEqual(self.client.get(self.item_path).json()["item"]["segments"], [])

    def test_observation_cannot_rebind_existing_work_id(self):
        before = self.client.get(self.item_path).json()["item"]
        result = self.client.post(self.item_path + "/observation", json=observation(canonical_url=URL2))
        self.assertEqual(result.status_code, 400)
        after = self.client.get(self.item_path).json()["item"]
        self.assertEqual(after["canonical_url"], before["canonical_url"])
        self.assertEqual(after["item_id"], before["item_id"])
        self.assertEqual(after["observations"], [])

    def test_concurrent_short_links_share_one_record_and_resolution_merges(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.client.post(BASE + "/import", json={"text": "分享给你 " + SHORT}).json()["item"]["item_id"], range(12)))
        self.assertEqual(len(set(results)), 1)
        short_id = results[0]
        response = self.client.post(BASE + "/items/" + short_id + "/observation", json={"canonical_url": URL})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["item_id"], self.item["item_id"])
        self.assertEqual(len(self.client.get(BASE + "/items").json()["items"]), 1)
        self.assertEqual(self.client.get(BASE + "/items/" + short_id).json()["item"]["item_id"], self.item["item_id"])

    def test_upload_magic_wins_over_filename_and_media_response_is_nosniff(self):
        result = self.client.post(self.item_path + "/media", params={"filename": "../../password.html"}, content=audio_bytes())
        self.assertEqual(result.status_code, 200)
        item = result.json()["item"]
        self.assertEqual(item["media_type"], "audio")
        self.assertEqual(item["media_url"], self.item_path + "/media")
        response = self.client.get(item["media_url"])
        self.assertEqual(response.content, audio_bytes())
        self.assertIn("audio/", response.headers["content-type"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        result = self.client.post(self.item_path + "/media", params={"filename": "video.mp4"}, content=b"<script>bad</script>")
        self.assertEqual(result.status_code, 400)
        self.assertEqual(len(self.client.get(self.item_path).json()["item"]["media"]), 1)

    def test_upload_size_and_storage_capacity_fail_before_writing(self):
        with patch("backend.app.douyin_api.MAX_UPLOAD_BYTES", 32):
            result = self.client.post(self.item_path + "/media", params={"filename": "speech.wav"}, content=audio_bytes())
        self.assertEqual(result.status_code, 413)
        self.store.max_storage_bytes = 10
        result = self.client.post(self.item_path + "/media", params={"filename": "speech.wav"}, content=audio_bytes())
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.store.storage_summary()["files"], 0)

    def test_unknown_item_and_arbitrary_media_ids_do_not_expose_files(self):
        self.assertEqual(self.client.get(BASE + "/items/does-not-exist").status_code, 404)
        result = self.client.get(self.item_path + "/media", params={"media_id": "../../catalog.db"})
        self.assertEqual(result.status_code, 404)
        result = self.client.post(BASE + "/items/does-not-exist/media", params={"filename": "speech.wav"}, content=audio_bytes())
        self.assertEqual(result.status_code, 404)
        self.assertEqual(self.store.storage_summary()["files"], 0)

    def test_nonlocal_and_private_origins_cannot_write(self):
        for origin in ("https://attacker.example", "http://192.168.18.80:5173", "http://10.0.0.2", "null", "http://localhost.attacker.example"):
            with self.subTest(origin=origin):
                result = self.client.post(BASE + "/import", json={"text": URL2}, headers={"Origin": origin})
                self.assertEqual(result.status_code, 403)
        for origin in ("http://127.0.0.1:5173", "http://localhost:5173", "http://[::1]:5173"):
            self.assertEqual(self.client.get(BASE + "/items", headers={"Origin": origin}).status_code, 200)
        self.assertEqual(len(self.store.list_items()), 1)

    def test_discovery_and_source_resume_are_honest_about_browser_execution(self):
        result = self.client.post(BASE + "/discover", json={"query": "AI解决实际工作问题", "max_results": 2})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["task"]["status"], "awaiting_browser")
        self.assertIn("尚未自动执行", result.json()["message"])
        self.store.set_status(self.item["item_id"], "waiting_login", "需本人登录")
        result = self.client.get(BASE + "/discovery/tasks")
        self.assertTrue(result.json()["source_state"]["paused"])
        response = self.client.post(BASE + "/source/resume")
        self.assertFalse(response.json()["source_state"]["paused"])
        self.assertIn("不表示登录或验证已成功", response.json()["message"])


if __name__ == "__main__":
    unittest.main()
