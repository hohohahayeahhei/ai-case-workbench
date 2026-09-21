"""Small offline evaluation harness for the case-verification policy."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .agent_model import AgentModel, MockAgentModel
from .db import normalize_url


NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")


def numeric_claim_is_grounded(case: Dict[str, Any]) -> bool:
    """Require every numeric token in an outcome claim to appear in evidence."""
    claim_numbers = set(NUMBER_RE.findall(case.get("outcome", {}).get("claim", "")))
    evidence = case.get("outcome", {}).get("evidence", "")
    return claim_numbers.issubset(set(NUMBER_RE.findall(evidence)))


def guarded_decision(case: Dict[str, Any], model_review: Dict[str, Any], seen_urls: Iterable[str]) -> Dict[str, str]:
    normalized = normalize_url(case["source_url"])
    if normalized in set(seen_urls):
        return {"decision": "reject", "reason": "规范化 URL 重复"}
    required = (case.get("problem", {}), case.get("approach", {}), case.get("outcome", {}))
    if not all(part.get("claim") and part.get("evidence") for part in required):
        return {"decision": "reject", "reason": "问题、方法或结果缺少原文证据"}
    if not numeric_claim_is_grounded(case):
        return {"decision": "reject", "reason": "结果数字没有出现在对应原文证据中"}
    if model_review.get("decision") != "pass":
        return {"decision": "reject", "reason": str(model_review.get("reason") or "模型核验未通过")}
    return {"decision": "pass", "reason": "模型判断通过且程序护栏通过"}


class UnsafeModel(MockAgentModel):
    """Test model that approves every case to expose the value of guardrails."""

    name = "unsafe-test-model"

    def structured(self, role: str, system: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if role == "verification":
            return {"decision": "pass", "reason": "故意模拟过度信任模型", "confidence": 1}
        return super().structured(role, system, payload)


def evaluate_fixture(fixture_path: str | Path, model: AgentModel | None = None) -> Dict[str, Any]:
    cases: List[Dict[str, Any]] = __import__("json").loads(Path(fixture_path).read_text(encoding="utf-8"))
    provider = model or MockAgentModel()
    rows: List[Dict[str, Any]] = []
    seen_urls: List[str] = []
    baseline_correct = guarded_correct = 0
    for case in cases:
        review = provider.structured(
            "verification",
            "你是核验 Agent，只依据原文证据输出 pass 或 reject。",
            {"case": case, "evidence_id": case["case_id"]},
        )
        expected = "pass" if case["expected_label"] == "pass" else "reject"
        baseline = "pass" if review.get("decision") == "pass" else "reject"
        guarded = guarded_decision(case, review, seen_urls)["decision"]
        seen_urls.append(normalize_url(case["source_url"]))
        baseline_correct += baseline == expected
        guarded_correct += guarded == expected
        rows.append({
            "case_id": case["case_id"],
            "expected": expected,
            "baseline": baseline,
            "guarded": guarded,
            "baseline_reason": review.get("reason", ""),
            "guarded_reason": guarded_decision(case, review, seen_urls[:-1])["reason"],
        })
    total = len(rows) or 1
    return {
        "model": provider.name,
        "dataset_size": len(rows),
        "baseline": {"accuracy": round(baseline_correct / total, 4)},
        "multi_agent_guarded": {"accuracy": round(guarded_correct / total, 4)},
        "rows": rows,
    }
