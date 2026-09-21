import tempfile
import unittest
from pathlib import Path

from backend.app.langgraph_workflow import LangGraphWorkbench


class LangGraphWorkflowTests(unittest.TestCase):
    def make_workbench(self, scenario="happy_path"):
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parents[2]
        workbench = LangGraphWorkbench(root, str(Path(temp_dir.name) / "business.db"), str(Path(temp_dir.name) / "checkpoints.db"), scenario)
        return temp_dir, workbench

    def test_run_pauses_for_human_review_and_resumes_with_same_thread(self):
        temp_dir, workbench = self.make_workbench()
        try:
            first = workbench.start("thread-approval")
            self.assertIn("__interrupt__", first)
            final = workbench.resume("thread-approval", "approve")
            self.assertEqual(final["publish_status"], "sent")
        finally:
            workbench.close()
            temp_dir.cleanup()

    def test_rejection_stops_before_sync(self):
        temp_dir, workbench = self.make_workbench()
        try:
            workbench.start("thread-reject")
            final = workbench.resume("thread-reject", "reject")
            self.assertEqual(final["approval"], "rejected")
            self.assertNotIn("sync_status", final)
        finally:
            workbench.close()
            temp_dir.cleanup()

    def test_sync_conflict_stops_before_publish(self):
        temp_dir, workbench = self.make_workbench("sync_conflict")
        try:
            workbench.start("thread-conflict")
            final = workbench.resume("thread-conflict", "approve")
            self.assertEqual(final["sync_status"], "sync_conflict")
            self.assertNotIn("publish_status", final)
        finally:
            workbench.close()
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
