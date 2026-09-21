import io
import json
import sqlite3
import tempfile
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from backend.app.douyin_media import MediaError, detect_media, transcribe_local
from backend.app.douyin_source import DouyinStore, is_douyin_url, normalize_douyin_link
from backend.app.publication_dates import today_local


URL = "https://www.douyin.com/video/7612345678901234567"
URL2 = "https://www.douyin.com/video/7612345678901234568"
SHORT = "https://v.douyin.com/AbCdEF123/"


def audio_bytes(samples=100):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * samples)
    return output.getvalue()


def evidence(kind="author_statement", role="workflow", **extras):
    return {"kind": kind, "role": role, "text": "操作者展示用 AI 汇总表格，并人工核对结果。", "start_seconds": 4, "end_seconds": 9, **extras}


def observation(**extras):
    return {"title": "标题中的夸张成效不是事实", "author": "公开作者",
            "published_at": today_local().isoformat(), "published_at_source": "browser_ui:published_label", **extras}


class DouyinStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = DouyinStore(self.root / "catalog.db", self.root / "media")
        self.item = self.store.import_link(URL)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_share_text_and_url_variants_deduplicate_by_work_id(self):
        duplicate = self.store.import_link("3.14 复制打开抖音 " + URL + "?from=share #AI")
        modal = self.store.import_link("https://www.douyin.com/?modal_id=7612345678901234567")
        self.assertEqual(duplicate["item_id"], modal["item_id"])
        self.assertEqual(len(self.store.list_items()), 1)
        self.assertEqual(modal["canonical_url"], URL)
        self.assertTrue(is_douyin_url("https://www.douyin.com/search/AI"))
        self.assertFalse(is_douyin_url("https://douyin.com.attacker.test/video/7612345678901234567"))

    def test_urls_cannot_escape_allowed_hosts_or_protocol(self):
        for value in ("file:///etc/passwd", "https://127.0.0.1/video/7612345678901234567", "https://www.douyin.com@evil.test/video/7612345678901234567",
                      "http://www.douyin.com/video/7612345678901234567", "https://www.douyin.com:8080/video/7612345678901234567", "https://v.douyin.com/../../"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_douyin_link(value)

    def test_title_and_metadata_never_become_body_or_verified_success(self):
        item = self.store.record_observation(self.item["item_id"], observation(metrics={"likes": "10万"}))
        document = self.store.evidence_document(item["item_id"])
        self.assertEqual(item["status"], "media_unavailable")
        self.assertEqual(document["text"], "")
        self.assertFalse(document["ready"])
        self.assertEqual(self.store.ready_items(), [])
        self.assertIn("metrics_observed_at", item["metadata"])

    def test_unknown_date_is_not_replaced_by_collection_date(self):
        item = self.store.record_observation(self.item["item_id"], {"title": "发布于最近"})
        self.assertIsNone(item["published_at"])
        self.assertEqual(item["status"], "date_unknown")
        with self.assertRaises(ValueError):
            self.store.record_observation(item["item_id"], observation(published_at_source="collected_at"))
        self.assertIsNone(self.store.get_item(item["item_id"])["published_at"])

    def test_old_and_future_dates_remain_out_of_scope(self):
        for date, state in (("2000-01-01", "outdated"), ((today_local() + timedelta(days=2)).isoformat(), "future")):
            item = self.store.record_observation(self.item["item_id"], observation(published_at=date, evidence=[evidence(), evidence("visible_frame", "outcome")]))
            self.assertEqual(item["status"], state)
            self.assertFalse(self.store.evidence_document(item["item_id"])["ready"])

    def test_only_narration_or_transcript_cannot_pass_results_gate(self):
        item = self.store.record_observation(self.item["item_id"], observation(evidence=[evidence(), evidence("machine_transcript", "outcome")]))
        self.assertEqual(item["status"], "evidence_insufficient")
        self.assertEqual(self.store.set_status(item["item_id"], "ready")["status"], "evidence_insufficient")
        machine = [entry for entry in item["evidence"] if entry["kind"] == "machine_transcript"][0]
        self.assertTrue(machine["machine_generated"])
        self.assertTrue(machine["uncertainty"])

    def test_random_or_unverified_machine_frame_cannot_prove_outcome(self):
        for extra in ({"role": "context"}, {"role": "outcome", "machine_generated": True}):
            self.store.record_observation(self.item["item_id"], observation(evidence=[evidence(), evidence("visible_frame", **extra)]))
            self.assertEqual(self.store.get_item(self.item["item_id"])["status"], "evidence_insufficient")

    def test_auditable_evidence_appends_deduplicates_and_links_time(self):
        first = observation(evidence=[evidence(), evidence("visible_frame", "outcome", text="屏幕展示已生成的对照表，示例数字 42 与输入一致。")])
        item = self.store.record_observation(self.item["item_id"], first)
        self.store.record_observation(item["item_id"], first)
        self.assertEqual(len(self.store.get_item(item["item_id"])["evidence"]), 2)
        self.assertEqual(item["status"], "ready")
        document = self.store.evidence_document(item["item_id"])
        self.assertIn("4–9秒", document["text"])
        self.assertIn("作者口述（未独立验证）", document["text"])
        self.assertNotIn("标题中的夸张成效", document["text"])
        self.assertTrue(document["untrusted_content"])
        self.assertEqual(len(self.store.ready_items()), 1)

    def test_evidence_requires_finite_time_and_correct_source(self):
        for invalid in (evidence(start_seconds=-1), evidence(end_seconds=float("nan")), evidence(end_seconds=3),
                        evidence(source_url=URL2), evidence(source_url="file:///etc/passwd"),
                        {"kind": "author_statement", "text": "没有时间位置"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.record_observation(self.item["item_id"], observation(evidence=[invalid]))
        self.assertEqual(self.store.get_item(self.item["item_id"])["evidence"], [])
        with self.assertRaises(ValueError):
            self.store.record_observation(self.item["item_id"], observation(evidence=[evidence("external_corroboration", "outcome", source_url=URL)]))

    def test_browser_observation_discards_unapproved_sensitive_fields(self):
        item = self.store.record_observation(self.item["item_id"], observation(cookie="DO-NOT-STORE", headers={"Authorization": "DO-NOT-STORE"}, raw_html="DO-NOT-STORE"))
        dump = "\n".join(self.store.connection.iterdump())
        self.assertNotIn("DO-NOT-STORE", dump)
        self.assertEqual(item["author"], "公开作者")

    def test_short_link_resolution_merges_metadata_evidence_media_and_aliases(self):
        short = self.store.import_link(SHORT, query="AI 表格")
        self.store.import_media(short["item_id"], audio_bytes(), "../voice.wav")
        self.store.record_observation(short["item_id"], observation(author="短链作者"))
        merged = self.store.record_observation(short["item_id"], {"canonical_url": URL})
        self.assertEqual(len(self.store.list_items()), 1)
        self.assertEqual(merged["item_id"], self.item["item_id"])
        self.assertIn(SHORT, merged["aliases"])
        self.assertEqual(merged["author"], "短链作者")
        self.assertEqual(merged["queries"], ["AI 表格"])
        self.assertEqual(len(merged["media"]), 1)
        self.assertEqual(self.store.import_link(SHORT)["item_id"], merged["item_id"])
        self.assertEqual(self.store.get_item(short["item_id"])["item_id"], merged["item_id"])

    def test_invalid_resolution_rolls_back_merge(self):
        short = self.store.import_link(SHORT)
        with self.assertRaises(ValueError):
            self.store.record_observation(short["item_id"], observation(canonical_url=URL, published_at_source="import date"))
        self.assertEqual(len(self.store.list_items()), 2)
        self.assertEqual(self.store.get_item(short["item_id"])["item_id"], short["item_id"])

    def test_short_alias_cannot_masquerade_as_independent_corroboration(self):
        short = self.store.import_link(SHORT)
        self.store.record_observation(short["item_id"], {"canonical_url": URL})
        with self.assertRaises(ValueError):
            self.store.record_observation(self.item["item_id"], observation(evidence=[evidence("external_corroboration", "outcome", source_url=SHORT)]))
        with self.assertRaises(ValueError):
            self.store.record_observation(self.item["item_id"], observation(evidence=[evidence("external_corroboration", "outcome", source_url="https://v.douyin.com/Unknown123/")]))

    def test_access_restrictions_pause_source_and_are_explicitly_resumable(self):
        for state in ("waiting_login", "manual_verification", "rate_limited"):
            self.store.set_status(self.item["item_id"], state, "真实浏览器限制")
            self.assertTrue(self.store.source_state()["paused"])
            self.assertEqual(self.store.pending_items(), [])
            self.assertEqual(self.store.request_discovery(query=state)["status"], state)
            self.store.resume_source()
            self.assertFalse(self.store.source_state()["paused"])
            self.assertEqual(self.store.get_item(self.item["item_id"])["status"], "awaiting_browser")

    def test_keyword_and_author_tasks_are_bounded_incremental_browser_queues(self):
        task = self.store.request_discovery(query="AI 表格", limit=2)
        self.assertEqual(task["status"], "awaiting_browser")
        self.assertIn("/search/", task["search_url"])
        done = self.store.complete_discovery(task["task_id"], [observation(canonical_url=URL)], duration_ms=123)
        self.assertEqual(done["progress"]["last_batch_count"], 1)
        self.assertEqual(done["progress"]["duration_ms"], 123)
        repeated = self.store.complete_discovery(task["task_id"], [observation(canonical_url=URL)])
        self.assertEqual(repeated["progress"]["item_ids"], [self.item["item_id"]])
        self.assertEqual(len(self.store.list_items()), 1)
        with self.assertRaises(ValueError):
            self.store.complete_discovery(task["task_id"], [observation(canonical_url=URL)] * 3)
        with self.assertRaises(ValueError):
            self.store.request_discovery(author_url="https://evil.example/user/123")

    def test_network_failures_backoff_and_stop_at_three_attempts(self):
        task = self.store.request_discovery(query="network retry")
        for attempt in range(3):
            item = self.store.set_status(self.item["item_id"], "network_error", "网络读取失败")
            task = self.store.complete_discovery(task["task_id"], [], status="network_error", reason="网络读取失败")
            self.assertEqual(item["metadata"]["network_attempts"], attempt + 1)
            self.assertEqual(self.store.pending_items(), [])
            self.assertEqual(self.store.pending_discovery_tasks(), [])
        self.assertFalse(task["progress"]["retryable"])
        self.assertIn("3 次重试上限", item["reason"])
        self.assertFalse(self.store.source_state()["paused"])

    def test_media_is_content_addressed_validated_and_cannot_read_arbitrary_paths(self):
        item = self.store.import_media(self.item["item_id"], audio_bytes(), "../../secret.html")
        first = self.store.media_path(item["item_id"])
        self.store.import_media(item["item_id"], audio_bytes(), "second.wav")
        self.assertEqual(len(self.store.get_item(item["item_id"])["media"]), 1)
        self.assertEqual(first.parent, (self.root / "media").resolve())
        self.assertTrue(first.name.endswith(".wav"))
        self.assertEqual(self.store.storage_summary()["files"], 1)
        with self.assertRaises(MediaError):
            self.store.import_media(item["item_id"], b"<script>alert(1)</script>", "video.mp4")
        with self.assertRaises(FileNotFoundError):
            self.store.media_path(item["item_id"], "../../etc/passwd")
        first.unlink()
        first.symlink_to(self.root / "catalog.db")
        with self.assertRaises(FileNotFoundError):
            self.store.media_path(item["item_id"])

    def test_capacity_limit_is_enforced_before_writing(self):
        self.store.max_storage_bytes = len(audio_bytes())
        self.store.import_media(self.item["item_id"], audio_bytes(), "first.wav")
        self.store.import_media(self.item["item_id"], audio_bytes(), "same.wav")
        with self.assertRaises(MediaError):
            self.store.import_media(self.item["item_id"], audio_bytes(101), "another.wav")
        self.assertEqual(len(list((self.root / "media").iterdir())), 1)

    def test_missing_transcription_runtime_records_failure_without_inventing_text(self):
        self.store.record_observation(self.item["item_id"], observation())
        self.store.import_media(self.item["item_id"], audio_bytes(), "speech.wav")
        with patch("backend.app.douyin_media.local_capabilities", return_value={"transcription_available": False}):
            item = self.store.process_media(self.item["item_id"])
        self.assertEqual(item["status"], "transcription_failed")
        self.assertIn("未配置本地转写模型", item["reason"])
        self.assertTrue(item["media_available"])
        self.assertEqual(self.store.evidence_document(item["item_id"])["text"], "")

    def test_transcript_cache_prevents_reprocessing_identical_media(self):
        self.store.record_observation(self.item["item_id"], observation())
        self.store.import_media(self.item["item_id"], audio_bytes(), "speech.wav")
        segment = evidence("machine_transcript", "context", engine="test-only")
        with patch("backend.app.douyin_source.transcribe_local", return_value=([segment], {"engine": "test-only"})) as transcriber:
            first = self.store.process_media(self.item["item_id"])
            second = self.store.process_media(self.item["item_id"])
        transcriber.assert_called_once()
        self.assertEqual(first["status"], "evidence_insufficient")
        self.assertEqual(len(second["evidence"]), 1)
        self.assertTrue(second["evidence"][0]["machine_generated"])

    def test_cleanup_deletes_media_but_preserves_evidence_and_trace(self):
        self.store.import_media(self.item["item_id"], audio_bytes(), "speech.wav")
        self.store.record_observation(self.item["item_id"], observation(evidence=[evidence()]))
        result = self.store.cleanup_media(target_bytes=0)
        self.assertEqual(result["deleted_count"], 1)
        item = self.store.get_item(self.item["item_id"])
        self.assertFalse(item["media_available"])
        self.assertEqual(len(item["evidence"]), 1)
        self.assertTrue(any(event["stage"] == "media_cleanup" for event in item["events"]))
        self.store.import_media(item["item_id"], audio_bytes(), "restored.wav")
        self.assertTrue(self.store.get_item(item["item_id"])["media_available"])

    def test_collection_outcome_is_separate_and_repeat_metadata_does_not_requeue(self):
        item = self.store.record_observation(self.item["item_id"], observation(evidence=[evidence(), evidence("visible_frame", "outcome")]))
        self.store.mark_processed(item["item_id"], "douyin-7612345678901234567", status="rejected")
        again = self.store.record_observation(item["item_id"], observation())
        self.assertEqual(again["status"], "processed")
        self.assertEqual(again["metadata"]["collection_status"], "rejected")
        self.assertEqual(self.store.ready_items(), [])
        repeated = self.store.record_observation(item["item_id"], observation(evidence=[evidence(), evidence("visible_frame", "outcome")]))
        self.assertEqual(repeated["status"], "processed")

    def test_interrupted_media_processing_can_resume_after_timeout(self):
        self.store.import_media(self.item["item_id"], audio_bytes(), "speech.wav")
        self.store.set_status(self.item["item_id"], "processing", "开始转写", "transcription")
        self.assertEqual(self.store.recover_stale_processing(), 0)
        self.store.connection.execute("UPDATE douyin_events SET created_at='2000-01-01T00:00:00+00:00' WHERE status='processing'")
        self.store.connection.commit()
        self.assertEqual(self.store.recover_stale_processing(), 1)
        item = self.store.get_item(self.item["item_id"])
        self.assertEqual(item["status"], "transcription_failed")
        self.assertTrue(item["media_available"])

    def test_concurrent_media_import_has_one_blob_and_links_duplicate_videos(self):
        second = self.store.import_link(URL2)
        another = DouyinStore(self.root / "catalog.db", self.root / "media")
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda n: (self.store if n % 2 else another).import_media(
                    self.item["item_id"] if n % 2 else second["item_id"], audio_bytes(), "voice.wav"), range(12)))
            self.assertEqual(self.store.storage_summary()["files"], 1)
            self.assertEqual(self.store.get_item(self.item["item_id"])["duplicate_video_item_ids"], [second["item_id"]])
        finally:
            another.close()

    def test_concurrent_connections_share_dedup_and_readonly_mode_does_not_mutate(self):
        another = DouyinStore(self.root / "catalog.db", self.root / "media")
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                values = list(pool.map(lambda n: (self.store if n % 2 else another).import_link(URL2)["item_id"], range(20)))
            self.assertEqual(len(set(values)), 1)
            self.assertEqual(len(self.store.list_items()), 2)
            reader = DouyinStore(self.root / "catalog.db", self.root / "media", readonly=True)
            try:
                self.assertEqual(len(reader.list_items()), 2)
                with self.assertRaises(sqlite3.OperationalError):
                    reader.import_link(URL)
            finally:
                reader.close()
        finally:
            another.close()


if __name__ == "__main__":
    unittest.main()
