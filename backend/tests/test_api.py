import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app.api import app, workbenches


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def tearDown(self):
        for run_id, workbench in list(workbenches.items()):
            workbench.close()
            del workbenches[run_id]

    def test_start_persists_awaiting_review_and_get_keeps_interrupt_state(self):
        response = self.client.post("/api/runs", json={"topic": "AI 产品案例", "scenario": "happy_path"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        run_id = payload["run"]["run_id"]
        self.assertTrue(payload["interrupted"])
        self.assertEqual(payload["run"]["status"], "awaiting_review")
        refreshed = self.client.get(f"/api/runs/{run_id}").json()
        self.assertTrue(refreshed["interrupted"])
        self.assertEqual(refreshed["run"]["status"], "awaiting_review")

    def test_reject_review_has_terminal_rejected_state(self):
        response = self.client.post("/api/runs", json={"topic": "AI 产品案例", "scenario": "happy_path"})
        run_id = response.json()["run"]["run_id"]
        reviewed = self.client.post(f"/api/runs/{run_id}/review", json={"decision": "reject"}).json()
        self.assertEqual(reviewed["run"]["status"], "rejected")
        self.assertFalse(reviewed["interrupted"])
        self.assertEqual(reviewed["deliveries"], [])

    def test_scenario_matrix(self):
        cases = {
            "happy_path": ("completed", {"synced", "sent"}),
            "sync_conflict": ("blocked", {"sync_conflict"}),
            "publish_unknown": ("needs_attention", {"synced", "unknown"}),
        }
        for scenario, (expected_run_status, expected_delivery_statuses) in cases.items():
            started = self.client.post("/api/runs", json={"scenario": scenario}).json()
            run_id = started["run"]["run_id"]
            final = self.client.post(f"/api/runs/{run_id}/review", json={"decision": "approve"}).json()
            self.assertEqual(final["run"]["status"], expected_run_status)
            self.assertEqual({row["status"] for row in final["deliveries"]}, expected_delivery_statuses)

    def test_recovery_endpoint_updates_external_stage(self):
        started = self.client.post("/api/runs", json={"scenario": "sync_conflict"}).json()
        run_id = started["run"]["run_id"]
        self.client.post(f"/api/runs/{run_id}/review", json={"decision": "approve"})
        recovered = self.client.post(f"/api/runs/{run_id}/recover", json={"action": "sync"}).json()
        self.assertEqual(recovered["run"]["status"], "completed")
        self.assertEqual({row["status"] for row in recovered["deliveries"]}, {"synced", "sent"})

    def test_official_connector_mode_survives_reload(self):
        started = self.client.post(
            "/api/runs",
            json={"scenario": "happy_path", "connector_mode": "official_mcp"},
        ).json()
        run_id = started["run"]["run_id"]
        self.assertEqual(started["run"]["connector_mode"], "official_mcp")
        workbench = workbenches.pop(run_id)
        workbench.close()
        refreshed = self.client.get(f"/api/runs/{run_id}").json()
        self.assertEqual(refreshed["run"]["connector_mode"], "official_mcp")
        self.assertEqual(self.client.get("/api/tools").json()["mode"], "official_mcp")

    def test_run_can_export_portfolio_report(self):
        started = self.client.post("/api/runs", json={"scenario": "happy_path"}).json()
        run_id = started["run"]["run_id"]
        self.client.post(f"/api/runs/{run_id}/review", json={"decision": "approve"})
        response = self.client.get(f"/api/runs/{run_id}/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn("AI 案例情报运行报告", response.text)
        self.assertIn("研究 Agent", response.text)
        self.assertIn("知识库", response.text)

    def test_history_lists_persisted_runs(self):
        started = self.client.post("/api/runs", json={"scenario": "happy_path"}).json()
        run_id = started["run"]["run_id"]
        response = self.client.get("/api/runs?limit=5")
        self.assertEqual(response.status_code, 200)
        self.assertIn(run_id, {item["run_id"] for item in response.json()["runs"]})

    def test_ready_probe_reports_local_dependencies(self):
        response = self.client.get("/api/readyz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")

    def test_public_config_never_returns_secret(self):
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("OPENAI_API_KEY", response.json())
        self.assertIn("llm_configured", response.json())

    def test_online_snapshot_runs_from_verified_source_fixture(self):
        started = self.client.post(
            "/api/runs",
            json={"dataset_mode": "online_snapshot", "scenario": "happy_path"},
        )
        self.assertEqual(started.status_code, 200)
        payload = started.json()
        self.assertEqual(payload["run"]["dataset_mode"], "online_snapshot")
        self.assertEqual(len(payload["cases"]), 3)
        self.assertTrue(all(case["source_url"].startswith("https://") for case in payload["cases"]))

    def test_tencent_docs_mode_fails_as_recorded_error_without_token(self):
        started = self.client.post(
            "/api/runs",
            json={"knowledge_mode": "tencent_docs", "scenario": "happy_path"},
        ).json()
        run_id = started["run"]["run_id"]
        self.assertEqual(started["run"]["status"], "awaiting_review")
        reviewed = self.client.post(f"/api/runs/{run_id}/review", json={"decision": "approve"}).json()
        self.assertEqual(reviewed["run"]["status"], "failed")
        self.assertTrue(any(event["event_type"] == "run_failed" for event in reviewed["events"]))

    def test_evaluation_endpoint_returns_baseline_and_guarded_reports(self):
        response = self.client.get("/api/evaluations/latest")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("baseline", payload["dataset"])
        self.assertIn("multi_agent_guarded", payload["adversarial"])


if __name__ == "__main__":
    unittest.main()
