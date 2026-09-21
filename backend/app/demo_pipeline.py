"""Deterministic end-to-end run used to validate the second project step."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.db import Database, normalize_url
from backend.app.mock_mcp import MockMCPRegistry
from backend.app.evaluation import numeric_claim_is_grounded


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "data" / "fixtures" / "cases.json"


def log(db: Database, run_id: str, actor: str, event_type: str, status: str, message: str, **metadata: Any) -> None:
    db.event(run_id, actor, event_type, status, message, metadata)
    print(f"[{status.upper():7}] {actor:12} {message}")


def run_demo(scenario: str = "happy_path", db_path: str = ":memory:") -> Dict[str, Any]:
    run_id = f"run-{scenario}"
    db = Database(db_path)
    db.init_schema()
    db.create_run(run_id, "AI 产品案例", scenario)
    registry = MockMCPRegistry(FIXTURES, db, scenario)

    log(db, run_id, "system", "run_started", "ok", "创建本地演示运行", scenario=scenario)
    candidates = registry.call("source", "search_sources", topic="AI 产品案例", max_results=3)
    log(db, run_id, "research_agent", "mcp_call", "ok", "Source MCP 返回 3 条候选", tool="search_sources", count=len(candidates))

    seen_urls: set[str] = set()
    for case in candidates:
        fetched = registry.call("source", "fetch_source", url=case["source_url"])
        evidence = registry.call("evidence", "save_evidence_bundle", case=fetched)
        normalized = normalize_url(case["source_url"])
        if normalized in seen_urls:
            status, reason = "rejected", "规范化 URL 与本次运行的已入选案例重复"
        elif not numeric_claim_is_grounded(fetched):
            status, reason = "rejected", "结果数字没有实验口径、样本或计算方式"
        else:
            status, reason = "verified", "问题、方法、结果均有原文证据，且未发现重复"
        seen_urls.add(normalized)
        db.save_case(run_id, case, status, reason)
        log(db, run_id, "verify_agent", "case_verified", "rejected" if status == "rejected" else "ok", f"{case['case_id']}：{status}，{reason}", evidence_id=evidence["evidence_id"])

    verified = list(db.verified_cases(run_id))
    body_lines = ["# AI 产品案例日报", "", f"主题：AI 产品案例", ""]
    for row in verified:
        case = json.loads(row["evidence_json"])
        body_lines.extend([
            f"## {case['title_original']}",
            f"- 原文：{case['source_url']}",
            f"- 问题：{case['problem']['claim']}",
            f"- 方法：{case['approach']['claim']}",
            f"- 结果：{case['outcome']['claim']}",
            "",
        ])
    body = "\n".join(body_lines).strip() + "\n"
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    version_id = f"content-{run_id}-v1"
    db.save_content_version(version_id, run_id, body, content_hash)
    log(db, run_id, "edit_agent", "content_frozen", "ok", "编辑 Agent 生成并冻结日报 v1", version_id=version_id, content_hash=content_hash)

    if not verified:
        db.update_run(run_id, "failed")
        return {"run_id": run_id, "status": "failed", "counts": db.counts(run_id)}

    sync_task = f"sync-{version_id}"
    db.upsert_delivery_task(sync_task, version_id, "knowledge_base", "demo-workspace")
    for row in verified:
        case = json.loads(row["evidence_json"])
        key = f"{version_id}:{case['case_id']}"
        registry.call("knowledge_base", "append_record", idempotency_key=key, record={"case_id": case["case_id"], "title": case["title_original"], "outcome": case["outcome"]["claim"]})
    conflict = False
    for row in verified:
        case = json.loads(row["evidence_json"])
        key = f"{version_id}:{case['case_id']}"
        remote = registry.call("knowledge_base", "read_record", record_id=key)
        expected = {"case_id": case["case_id"], "title": case["title_original"], "outcome": case["outcome"]["claim"]}
        if remote != expected:
            conflict = True
            log(db, run_id, "sync_executor", "readback", "conflict", f"{case['case_id']} 回读内容不一致，暂停后续发布", expected=expected, actual=remote)
    if conflict:
        db.update_delivery(sync_task, "sync_conflict", error="回读内容与冻结版本不一致")
        db.update_run(run_id, "blocked")
        return {"run_id": run_id, "status": "blocked", "counts": db.counts(run_id), "content_hash": content_hash}
    db.update_delivery(sync_task, "synced")
    log(db, run_id, "sync_executor", "readback", "ok", "知识库写入并精确回读通过", task_id=sync_task)

    publish_task = f"publish-{version_id}"
    db.upsert_delivery_task(publish_task, version_id, "demo_channel", "demo-target")
    sent = registry.call("distribution", "send_message", task_id=publish_task, body_hash=content_hash)
    db.update_delivery(publish_task, "unknown" if sent["status"] == "unknown" else "sent", external_id=sent["external_id"])
    receipt = registry.call("distribution", "get_delivery_receipt", external_id=sent["external_id"])
    final_status = "unknown" if receipt["status"] == "unknown" else "sent"
    db.receipt(publish_task, final_status, json.dumps(receipt, ensure_ascii=False))
    db.update_run(run_id, "completed" if final_status == "sent" else "needs_attention")
    log(db, run_id, "publish_executor", "receipt", "ok" if final_status == "sent" else "pending", f"发布回执：{final_status}", external_id=sent["external_id"])
    return {"run_id": run_id, "status": "completed" if final_status == "sent" else "needs_attention", "counts": db.counts(run_id), "content_hash": content_hash}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["happy_path", "sync_conflict", "publish_unknown"], default="happy_path")
    parser.add_argument("--db", default=":memory:")
    args = parser.parse_args()
    print(json.dumps(run_demo(args.scenario, args.db), ensure_ascii=False, indent=2))
