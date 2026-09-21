import tempfile
import unittest
from pathlib import Path

from backend.app.langgraph_workflow import LangGraphWorkbench


class OfficialMCPWorkflowTests(unittest.TestCase):
    def test_workflow_can_use_official_mcp_stdio_connector(self):
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parents[2]
        state_path = Path("/tmp/ai-case-workbench-mcp-state.json")
        state_path.unlink(missing_ok=True)
        workbench = LangGraphWorkbench(
            root,
            str(Path(temp_dir.name) / "business.db"),
            str(Path(temp_dir.name) / "checkpoints.db"),
            connector_mode="official_mcp",
        )
        try:
            first = workbench.start("official-mcp-workflow")
            self.assertIn("__interrupt__", first)
            final = workbench.resume("official-mcp-workflow", "approve")
            self.assertEqual(final["publish_status"], "sent")
        finally:
            workbench.close()
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
