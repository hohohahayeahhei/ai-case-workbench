import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from backend.app.db import Database
from backend.app import notifications as n
from backend.app.publication_dates import today_local
from backend.tests.quality_fixtures import approve


WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key-12345678"


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.loader = patch.object(n, 'load_local_env')
        self.loader.start()
        self.addCleanup(self.loader.stop)
        self.db = Database()
        self.db.init_schema()
        self.transport = patch.object(n, "_post_webhook")
        self.send = self.transport.start()
        self.addCleanup(self.transport.stop)
        self.addCleanup(self.db.close)

    def item(self, name="case-1", status="selected", level=3, **changes):
        item = {"case_id": name, "title_original": "用 AI 整理旅行手记和照片",
                "source_url": f"https://example.com/{name}", "source_type": "blog",
                "discovery_mode": "web_search", "published_at": today_local().isoformat(),
                "problem": {"claim": "作者需要将多日旅行笔记和照片整理成方便分享的旅行书", "evidence": "旅行笔记"},
                "approach": {"claim": "作者逐章向模型输入笔记和照片并人工修改模型草稿", "evidence": "使用模型整理"},
                "outcome": {"claim": "作者完成了旅行书草稿并分享了制作流程", "evidence": "完成草稿"},
                "limitations": []}
        item.update(changes)
        approve(item, level)
        self.db.save_source_item(item, status=status)
        return item

    def result(self, *ids, run_id="collect-test", status="completed"):
        return {"run_id": run_id, "status": status, "source_mode": "live",
                "finished_at": "2026-09-21T01:00:00+00:00", "items": [{"id": name} for name in ids]}

    def notify(self, result):
        return n.notify_collection(self.db, result, webhook_url=WEBHOOK)

    def due(self):
        self.db.connection.execute("UPDATE notification_outbox SET retry_after=0")
        self.db.connection.commit()

    def test_disabled_by_default_and_config_requires_opt_in(self):
        with patch.object(n, "load_local_env") as loader, patch.dict("os.environ", {}, clear=True):
            self.assertEqual(n.notify_collection(self.db, self.result()), {"status": "disabled"})
            loader.assert_called_once()
        self.send.assert_not_called()
        with patch.object(n, "load_local_env"), patch.dict("os.environ", {
            "AI_CASE_NOTIFICATIONS_ENABLED": "true", "AI_CASE_WECOM_WEBHOOK_URL": WEBHOOK}, clear=True):
            self.assertEqual(n.notify_collection(self.db, self.result())["status"], "sent")

    def test_only_this_run_live_verified_recent_and_approved_records_are_sent(self):
        self.item("keep")
        self.item("elsewhere")
        self.item("fixture", discovery_mode="fixture")
        self.item("old", published_at="2000-01-01")
        self.item("unknown_date", published_at=None)
        self.item("future", published_at="2999-01-01")
        self.item("candidate", status="candidate")
        self.item("low_score", level=1)
        self.item("rejected", verification={"decision": "reject"})
        stale = self.item("stale")
        stale["approach"]["claim"] += "新增未经核验的细节"
        self.db.save_source_item(stale, status="selected")
        result = self.result("keep", "fixture", "old", "unknown_date", "future", "candidate", "low_score", "rejected", "stale", "absent")
        preview = n.preview_collection(self.db, result)
        self.assertEqual(preview["case_ids"], ["keep"])
        self.assertIn("方法：", preview["content"])
        self.assertIn("结果：", preview["content"])
        self.assertEqual(self.notify(result)["item_count"], 1)
        self.assertIn("https://example.com/keep", self.send.call_args.args[1])

    def test_quality_sort_max_five_and_utf8_budget(self):
        for i in range(8):
            self.item(str(i), level=4 if i == 7 else 3,
                      title_original="超长标题*[]<>" * 100,
                      approach={"claim": "模型方法说明" * 200, "evidence": "方法"})
        preview = n.preview_collection(self.db, self.result(*(str(i) for i in range(8))))
        self.assertGreater(preview["item_count"], 0)
        self.assertLessEqual(preview["item_count"], 5)
        self.assertEqual(preview["case_ids"][0], "7")
        self.assertLessEqual(len(preview["content"].encode("utf-8")), 4096)
        self.assertIn(r"\*\[\]", preview["content"])
        self.assertIn("&lt;&gt;", preview["content"])

    def test_synopsis_keeps_complete_method_and_outcome(self):
        item = self.item(approach={"claim": "作者自述在 Claude 应用中点击 Dispatch，要求 Claude 浏览个人信息流，结合点赞与评论筛选值得写作的帖子并给出写作角度。", "evidence": "流程"},
                         outcome={"claim": "作者表示，Claude 给出了筛选后的帖子和对应写作角度，帮助作者确定后续选题。", "evidence": "结果"})
        description = n._description(item)
        self.assertNotIn("作者自述", description)
        self.assertNotIn("作者表示", description)
        self.assertIn("筛选值得写作的帖子并给出写作角度。", description)
        self.assertIn("帮助作者确定后续选题。", description)
        self.assertNotIn("…", description)
        self.assertLessEqual(len(description), 180)
        self.assertTrue(n._synopsis("完整分句足够长" * 8 + "，" + "后续内容" * 40).endswith("。"))

    def test_digest_never_selects_more_than_five(self):
        for i in range(8):
            self.item(str(i))
        self.assertEqual(n.preview_collection(self.db, self.result(*(str(i) for i in range(8))))["item_count"], 5)

    def test_long_unicode_links_still_fit_budget(self):
        for i in range(7):
            self.item(str(i), source_url=f"https://example.com/{i}/" + "中" * 90)
        result = n.preview_collection(self.db, self.result(*(str(i) for i in range(7))))
        self.assertGreater(result["item_count"], 0)
        self.assertLessEqual(len(result["content"].encode("utf-8")), n.MAX_BYTES)

    def test_unsafe_source_links_are_omitted_and_markdown_link_encoded(self):
        self.item("unsafe", source_url="javascript:alert(1)")
        self.item("local", source_url="http://127.0.0.1/private")
        self.item("auth", source_url="https://name:secret@example.com/")
        self.item("valid", source_url="https://example.com/(case)?x=[yes]")
        result = n.preview_collection(self.db, self.result("unsafe", "local", "auth", "valid"))
        self.assertEqual(result["case_ids"], ["valid"])
        self.assertIn("%28case%29", result["content"])
        self.assertIn("%5Byes%5D", result["content"])

    def test_run_is_sent_once_and_preview_preserves_frozen_content(self):
        self.item()
        result = self.result("case-1")
        self.assertEqual(self.notify(result)["status"], "sent")
        original = self.send.call_args.args[1]
        self.item(title_original="later edit")
        self.assertEqual(self.notify(result)["attempts"], 1)
        self.send.assert_called_once()
        preview = n.preview_collection(self.db, result, webhook_url=WEBHOOK)
        self.assertEqual(preview["content"], original)

    def test_successful_cases_deduplicate_across_runs_and_tracking_urls(self):
        self.item("first", source_url="https://example.com/story?utm_source=one")
        self.notify(self.result("first"))
        self.item("rediscovered", source_url="https://example.com/story?utm_source=two#heading")
        sent = self.notify(self.result("rediscovered", run_id="next-run"))
        self.assertEqual(sent["item_count"], 0)
        self.assertIn("今日暂无新增达标案例", self.send.call_args.args[1])

    def test_failed_and_partial_collection_statuses_are_honest(self):
        self.notify(self.result(status="failed"))
        self.assertIn("今日采集失败", self.send.call_args.args[1])
        self.assertNotIn("今日暂无新增达标案例", self.send.call_args.args[1])
        self.notify(self.result(run_id="partial", status="partial"))
        self.assertIn("部分完成", self.send.call_args.args[1])
        self.assertIn("不代表今日没有案例", self.send.call_args.args[1])
        self.item()
        self.notify(self.result("case-1", run_id="partial-with-case", status="partial"))
        self.assertIn("查看原文", self.send.call_args.args[1])
        self.assertEqual(self.notify(self.result(run_id="running", status="running"))["status"], "skipped")
        self.notify(self.result(run_id="interrupted", status="interrupted"))
        self.assertIn("今日采集中断", self.send.call_args.args[1])

    def test_digest_uses_shanghai_date_and_marks_explicit_connection_test(self):
        result = {**self.result(), "finished_at": "2026-09-21T19:30:00+00:00", "notification_test": True}
        preview = n.preview_collection(self.db, result)
        self.assertIn("接入测试 · AI 案例简报 · 2026-09-22", preview["content"])
        self.assertNotIn("接入测试", n.preview_collection(self.db, self.result())["content"])

    def test_invalid_webhook_never_sends_or_echoes_key(self):
        for url in [WEBHOOK.replace("https:", "http:"), WEBHOOK.replace("qyapi.weixin.qq.com", "evil.example"),
                    WEBHOOK.replace("/send", "/upload_media"), WEBHOOK + "&other=secret", WEBHOOK + "#secret",
                    WEBHOOK.replace("qyapi", "user:secret@qyapi"), WEBHOOK.replace("test-key-12345678", "")]:
            result = n.notify_collection(self.db, self.result(), webhook_url=url)
            self.assertEqual(result, {"status": "failed", "error": "invalid_webhook_configuration"})
        self.send.assert_not_called()

    def test_explicit_rejection_is_retried_after_cooldown_with_frozen_content(self):
        self.item()
        self.send.side_effect = n._DeliveryError("platform_error_45009")
        result = self.notify(self.result("case-1"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(n.retry_pending(self.db, webhook_url=WEBHOOK), [])
        original = self.send.call_args.args[1]
        self.item(title_original="modified")
        self.due()
        self.send.side_effect = None
        result = n.retry_pending(self.db, webhook_url=WEBHOOK)
        self.assertEqual(result[0]["status"], "sent")
        self.assertEqual(result[0]["attempts"], 2)
        self.assertEqual(self.send.call_args.args[1], original)

    def test_retry_budget_and_superseded_digest_avoid_repetition(self):
        self.item()
        self.send.side_effect = n._DeliveryError("platform_error_45009")
        self.notify(self.result("case-1"))
        for _ in range(4):
            self.due()
            n.retry_pending(self.db, webhook_url=WEBHOOK)
        self.assertEqual(self.send.call_count, 3)
        self.send.side_effect = None
        self.assertEqual(self.notify(self.result("case-1", run_id="later"))["status"], "sent")

        self.item("another")
        self.send.side_effect = n._DeliveryError("platform_error_45009")
        self.notify(self.result("another", run_id="old-digest"))
        self.send.side_effect = None
        self.notify(self.result("another", run_id="new-digest"))
        self.due()
        result = n.retry_pending(self.db, webhook_url=WEBHOOK, run_id="old-digest")
        self.assertEqual(result[0]["status"], "superseded")

    def test_timeout_or_unexpected_error_does_not_leak_key_or_retry(self):
        self.item()
        self.send.side_effect = TimeoutError(WEBHOOK)
        result = self.notify(self.result("case-1"))
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("test-key", json.dumps(result))
        self.assertEqual(n.retry_pending(self.db, webhook_url=WEBHOOK), [])
        self.notify(self.result("case-1"))
        self.send.assert_called_once()
        row = dict(self.db.connection.execute("SELECT * FROM notification_outbox").fetchone())
        self.assertNotIn("test-key", json.dumps(row))
        self.send.side_effect = None
        self.assertEqual(self.notify(self.result("case-1", run_id="next"))["item_count"], 0)

    def test_expired_claim_requires_confirmation_without_resending(self):
        self.notify(self.result())
        self.db.connection.execute("UPDATE notification_outbox SET status='sending',lease_until=0")
        self.db.connection.commit()
        result = self.notify(self.result())
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"], "interrupted_delivery_unknown")
        self.send.assert_called_once()

    def test_concurrent_same_run_sends_only_once_across_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            owner = Database(path)
            owner.init_schema()
            owner.close()
            barrier = threading.Barrier(6)
            def attempt(_):
                db = Database(path)
                try:
                    barrier.wait(timeout=5)
                    return n.notify_collection(db, self.result(), webhook_url=WEBHOOK)
                finally:
                    db.close()
            with ThreadPoolExecutor(max_workers=6) as workers:
                results = list(workers.map(attempt, range(6)))
            self.assertTrue(all(result["status"] in {"sent", "sending"} for result in results), results)
            self.send.assert_called_once()


class WebhookTransportTests(unittest.TestCase):
    def post(self, *, status=200, payload=None, error=None):
        response = Mock()
        response.status = status
        response.read.return_value = json.dumps(payload if payload is not None else {"errcode": 0}).encode()
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        if error:
            opener.open.side_effect = error
        with patch.object(n, "build_opener", return_value=opener) as build:
            n._post_webhook(WEBHOOK, "中文简报")
        return opener, build

    def test_success_validates_errcode_and_uses_bounded_no_redirect_transport(self):
        opener, build = self.post()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data)["markdown"]["content"], "中文简报")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 12)
        self.assertIsInstance(build.call_args.args[0], n._NoRedirect)
        self.assertIsNone(n._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_http_platform_timeout_and_malformed_response_fail_safely(self):
        for args, code, unknown in [
            ({"payload": {"errcode": 40000, "errmsg": WEBHOOK}}, "platform_error_40000", False),
            ({"status": 500}, "http_status_500", True),
            ({"payload": {}}, "invalid_response", True),
            ({"payload": {"errcode": False}}, "invalid_response", True),
            ({"error": TimeoutError(WEBHOOK)}, "network_delivery_unknown", True),
            ({"error": URLError(WEBHOOK)}, "network_delivery_unknown", True),
            ({"error": HTTPError(WEBHOOK, 302, "redirect", {}, None)}, "http_status_302", False),
        ]:
            with self.subTest(code=code), self.assertRaises(n._DeliveryError) as caught:
                self.post(**args)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.unknown, unknown)
            self.assertNotIn("test-key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
