import tempfile
import unittest
from pathlib import Path

from backend.app.langgraph_workflow import LangGraphWorkbench


class AdversarialModel:
    name = "adversarial-test-model"

    def structured(self, role, system, payload):
        if role == "research":
            return {"selected_case_ids": [case["case_id"] for case in payload["candidates"]]}
        if role == "verification":
            return {"decision": "pass", "reason": "模型故意放行", "confidence": 1}
        if role == "editor":
            return {"items": [{"case_id": "demo_case_001", "title": "模型编造的标题", "source_url": "https://fake.example", "problem": "编造问题", "approach": "编造方法", "outcome": "编造结果"}]}
        raise AssertionError(role)


class AgentGuardrailTests(unittest.TestCase):
    def test_program_guardrails_reject_unsupported_metric_and_fallback_editor(self):
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parents[2]
        workbench = LangGraphWorkbench(root, str(Path(temp_dir.name) / "business.db"), str(Path(temp_dir.name) / "checkpoints.db"), model=AdversarialModel())
        try:
            first = workbench.start("adversarial-001")
            self.assertIn("__interrupt__", first)
            rows = workbench.db.cases("adversarial-001")
            statuses = {row["case_id"]: row["status"] for row in rows}
            self.assertEqual(statuses["demo_case_001"], "verified")
            self.assertEqual(statuses["demo_case_002"], "rejected")
            version = workbench.db.content_version("adversarial-001")
            self.assertIn("示例：客服团队用 AI 助手缩短知识检索时间", version["body"])
            self.assertNotIn("模型编造的标题", version["body"])
        finally:
            workbench.close()
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
