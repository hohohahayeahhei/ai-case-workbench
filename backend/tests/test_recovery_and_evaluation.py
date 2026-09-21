import tempfile
import unittest
from pathlib import Path

from backend.app.evaluation import UnsafeModel, evaluate_fixture
from backend.app.langgraph_workflow import LangGraphWorkbench


class RecoveryAndEvaluationTests(unittest.TestCase):
    def make_workbench(self, scenario):
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parents[2]
        workbench = LangGraphWorkbench(root, str(Path(temp_dir.name) / "business.db"), str(Path(temp_dir.name) / "checkpoints.db"), scenario)
        return temp_dir, workbench

    def test_sync_conflict_retries_only_external_stage(self):
        temp_dir, workbench = self.make_workbench("sync_conflict")
        try:
            workbench.start("recover-sync")
            blocked = workbench.resume("recover-sync", "approve")
            self.assertEqual(blocked["sync_status"], "sync_conflict")
            recovered = workbench.recover("recover-sync", "sync")
            self.assertEqual(recovered["publish_status"], "sent")
            events = workbench.db.events("recover-sync")
            self.assertEqual(sum(row["event_type"] == "node_started" and row["actor"] == "research_agent" for row in events), 1)
            self.assertEqual(sum(row["event_type"] == "recovery_started" for row in events), 1)
        finally:
            workbench.close(); temp_dir.cleanup()

    def test_unknown_publish_can_be_resolved_by_receipt_query(self):
        temp_dir, workbench = self.make_workbench("publish_unknown")
        try:
            workbench.start("recover-publish")
            workbench.resume("recover-publish", "approve")
            recovered = workbench.recover("recover-publish", "publish")
            self.assertEqual(recovered["publish_status"], "sent")
        finally:
            workbench.close(); temp_dir.cleanup()

    def test_guarded_evaluation_beats_unsafe_baseline(self):
        root = Path(__file__).resolve().parents[2]
        report = evaluate_fixture(root / "data/fixtures/cases.json", UnsafeModel())
        self.assertLess(report["baseline"]["accuracy"], report["multi_agent_guarded"]["accuracy"])
        self.assertEqual(report["multi_agent_guarded"]["accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main()
