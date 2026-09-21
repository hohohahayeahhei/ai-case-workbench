"""LangGraph workflow with SQLite checkpointing and a human review interrupt."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .db import Database, normalize_url
from .agent_model import AgentModel, create_agent_model
from .evaluation import numeric_claim_is_grounded
from .mock_mcp import MockMCPRegistry
from .official_mcp_registry import OfficialMCPRegistry
from .tencent_docs_registry import TencentDocsRegistry
from .scoring import score_case


class WorkbenchState(TypedDict, total=False):
    run_id: str
    topic: str
    scenario: str
    candidates: List[Dict[str, Any]]
    verified_case_ids: List[str]
    rejected_case_ids: List[str]
    content_version_id: str
    content_hash: str
    body: str
    approval: str
    sync_status: str
    publish_status: str
    error: str
    halt: bool


class LangGraphWorkbench:
    def __init__(self, project_root: str | Path, business_db_path: str, checkpoint_db_path: str, scenario: str = "happy_path", model: AgentModel | None = None, connector_mode: str = "mock", llm_mode: str = "mock", run_id: str | None = None, dataset_mode: str = "fixture", knowledge_mode: str = "local_sqlite") -> None:
        self.root = Path(project_root)
        self.db = Database(business_db_path)
        self.db.init_schema()
        self.scenario = scenario
        self.llm_mode = llm_mode
        if dataset_mode not in {"fixture", "online_snapshot"}:
            raise ValueError(f"Unsupported dataset mode: {dataset_mode}")
        self.dataset_mode = dataset_mode
        if knowledge_mode not in {"local_sqlite", "tencent_docs"}:
            raise ValueError(f"Unsupported knowledge mode: {knowledge_mode}")
        self.knowledge_mode = knowledge_mode
        # Respect the run's explicit mode. This keeps tests and offline demos
        # deterministic even when a real provider is configured in .env.
        self.model = model or create_agent_model(llm_mode)
        if connector_mode not in {"mock", "official_mcp"}:
            raise ValueError(f"Unsupported connector mode: {connector_mode}")
        self.connector_mode = connector_mode
        self.checkpoint_connection = sqlite3.connect(checkpoint_db_path, check_same_thread=False)
        self.checkpointer = SqliteSaver(self.checkpoint_connection)
        self.checkpointer.setup()
        self.registry = (
            TencentDocsRegistry(self.root, scenario, self._fixture_path())
            if knowledge_mode == "tencent_docs"
            else OfficialMCPRegistry(self.root, scenario, fixture_path=self._fixture_path())
            if connector_mode == "official_mcp"
            else MockMCPRegistry(self._fixture_path(), self.db, scenario)
        )
        if run_id and hasattr(self.registry, "bind_run"):
            self.registry.bind_run(run_id)
        self.graph = self._build_graph()

    def close(self) -> None:
        if hasattr(self.registry, "close"):
            self.registry.close()
        self.checkpoint_connection.close()
        self.db.close()

    def _fixture_path(self) -> Path:
        return self.root / "data/fixtures/online_cases.json" if self.dataset_mode == "online_snapshot" else self.root / "data/fixtures/cases.json"

    def _event(self, state: WorkbenchState, actor: str, event_type: str, status: str, message: str, **metadata: Any) -> None:
        self.db.event(state["run_id"], actor, event_type, status, message, metadata)

    def _build_graph(self):
        builder = StateGraph(WorkbenchState)
        builder.add_node("research", self.research)
        builder.add_node("verify", self.verify)
        builder.add_node("edit_and_freeze", self.edit_and_freeze)
        builder.add_node("human_review", self.human_review)
        builder.add_node("sync", self.sync)
        builder.add_node("publish", self.publish)
        builder.add_edge(START, "research")
        builder.add_edge("research", "verify")
        builder.add_edge("verify", "edit_and_freeze")
        builder.add_conditional_edges("edit_and_freeze", self.route_after_edit, {"review": "human_review", "end": END})
        builder.add_conditional_edges("human_review", self.route_after_review, {"sync": "sync", "end": END})
        builder.add_conditional_edges("sync", self.route_after_sync, {"publish": "publish", "end": END})
        builder.add_edge("publish", END)
        return builder.compile(checkpointer=self.checkpointer)

    def research(self, state: WorkbenchState) -> Dict[str, Any]:
        run_id = state["run_id"]
        self._event(state, "research_agent", "node_started", "ok", "研究 Agent 开始检索")
        candidates = self.registry.call("source", "search_sources", topic=state["topic"], max_results=3)
        self._event(state, "research_agent", "mcp_call", "ok", "调用 Source MCP.search_sources", count=len(candidates))
        for candidate in candidates:
            self.db.save_source_item(candidate)
        selection = self.model.structured(
            "research",
            '你是研究 Agent。只能从候选列表中选择案例 ID，不得创造案例、URL 或事实。输出 JSON：{"selected_case_ids":["case_id"]}。',
            {"topic": state["topic"], "candidates": candidates},
        )
        selected_ids = {str(value) for value in selection.get("selected_case_ids", [])}
        selected = [case for case in candidates if case["case_id"] in selected_ids]
        self._event(state, "research_agent", "model_call", "ok", "研究 Agent 输出结构化候选选择", model=self.model.name, selected=len(selected))
        return {"candidates": selected}

    def verify(self, state: WorkbenchState) -> Dict[str, Any]:
        verified: List[str] = []
        rejected: List[str] = []
        seen_urls = set()
        for case in state["candidates"]:
            fetched = self.registry.call("source", "fetch_source", url=case["source_url"])
            evidence = self.registry.call("evidence", "save_evidence_bundle", case=fetched)
            url = normalize_url(case["source_url"])
            model_review = self.model.structured(
                "verification",
                "你是核验 Agent。必须只依据给出的原文证据判断，不得相信其他 Agent 的结论。输出 decision=pass 或 reject 和 reason。",
                {"case": fetched, "evidence_id": evidence["evidence_id"]},
            )
            duplicate = self.registry.call("evidence", "check_duplicate_case", url=case["source_url"], title=case["title_original"])
            if url in seen_urls:
                decision, reason = "rejected", "规范化 URL 重复"
            elif not _has_required_evidence(fetched) or not numeric_claim_is_grounded(fetched):
                decision, reason = "rejected", "结果数字缺少可验证出处"
            elif model_review.get("decision") != "pass":
                decision, reason = "rejected", str(model_review.get("reason") or "核验 Agent 未通过")
            else:
                decision, reason = "verified", "问题、方法、结果均有原文证据"
            seen_urls.add(url)
            cluster_id = _cluster_id(case)
            try:
                model_scorecard = self.model.structured(
                    "scoring",
                    "你是内容评分 Agent。只根据给出的案例证据写一句推荐理由，不改变事实，不输出总分。",
                    {"case": fetched},
                )
            except Exception:
                model_scorecard = {}
            scorecard = score_case(fetched, model_scorecard)
            self.db.save_case(state["run_id"], case, decision, reason, scorecard, cluster_id)
            self.db.save_cluster(cluster_id, case["case_id"], case["title_original"], [case["case_id"]])
            self._event(state, "verify_agent", "model_call", "ok", f"核验 Agent 审阅 {case['case_id']}", model=self.model.name, model_decision=model_review.get("decision"))
            self._event(state, "verify_agent", "case_verified", "rejected" if decision == "rejected" else "ok", f"{case['case_id']}：{decision}，{reason}", evidence_id=evidence["evidence_id"])
            self._event(state, "scoring_agent", "scorecard_created", "ok", f"{case['case_id']}：质量分 {scorecard['quality_score']}，{scorecard['tier']}", score_version=scorecard["score_version"], quality_score=scorecard["quality_score"], tier=scorecard["tier"], hard_flags=scorecard["hard_flags"])
            (verified if decision == "verified" else rejected).append(case["case_id"])
        return {"verified_case_ids": verified, "rejected_case_ids": rejected, "halt": not verified}

    def edit_and_freeze(self, state: WorkbenchState) -> Dict[str, Any]:
        rows = self.db.verified_cases(state["run_id"])
        verified_cases = [json.loads(row["evidence_json"]) for row in rows]
        if not verified_cases:
            self.db.update_run(state["run_id"], "needs_evidence")
            self._event(state, "system", "no_publishable_cases", "blocked", "没有通过核验的案例，停止生成和发布空日报")
            return {"halt": True, "error": "没有通过核验的案例"}
        model_draft = self.model.structured(
            "editor",
            "你是编辑 Agent。只能复制 verified_cases 中的 title、source_url、problem.claim、approach.claim、outcome.claim。不得添加数字、结论或事实。输出 JSON。",
            {"topic": state["topic"], "verified_cases": verified_cases},
        )
        draft_items = model_draft.get("items", [])
        if not _draft_is_grounded(draft_items, verified_cases):
            self._event(state, "edit_agent", "guardrail", "fallback", "模型草稿未通过证据字段校验，使用程序安全渲染")
            draft_items = [_safe_editor_item(case) for case in verified_cases]
        self._event(state, "edit_agent", "model_call", "ok", "编辑 Agent 输出结构化草稿", model=self.model.name, item_count=len(draft_items))
        lines = ["# AI 产品案例日报", "", f"主题：{state['topic']}", ""]
        for item in draft_items:
            lines.extend([
                f"## {item['title']}",
                f"- 原文：{item['source_url']}",
                f"- 问题：{item['problem']}",
                f"- 方法：{item['approach']}",
                f"- 结果：{item['outcome']}",
                "",
            ])
        body = "\n".join(lines).strip() + "\n"
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        version_id = f"content-{state['run_id']}-v1"
        self.db.save_content_version(version_id, state["run_id"], body, content_hash)
        self._event(state, "edit_agent", "content_frozen", "ok", "编辑 Agent 生成并冻结日报", content_version_id=version_id)
        return {"content_version_id": version_id, "content_hash": content_hash, "body": body}

    def human_review(self, state: WorkbenchState) -> Dict[str, Any]:
        # Persist the waiting state before interrupting so a refreshed UI can
        # distinguish "awaiting review" from a still-running graph.
        self.db.update_run(state["run_id"], "awaiting_review")
        decision = interrupt({
            "type": "content_review",
            "message": "请审核冻结日报后决定是否继续同步和发布。",
            "content_version_id": state["content_version_id"],
            "content_hash": state["content_hash"],
            "verified_case_ids": state["verified_case_ids"],
        })
        approved = decision in (True, "approve", "approved") or (isinstance(decision, dict) and decision.get("action") == "approve")
        self._event(state, "human_reviewer", "approval", "ok" if approved else "rejected", "审核结果：" + ("approved" if approved else "rejected"))
        self.db.update_run(state["run_id"], "running" if approved else "rejected")
        return {"approval": "approved" if approved else "rejected"}

    @staticmethod
    def route_after_review(state: WorkbenchState) -> str:
        return "sync" if state.get("approval") == "approved" else "end"

    @staticmethod
    def route_after_edit(state: WorkbenchState) -> str:
        return "end" if state.get("halt") else "review"

    def sync(self, state: WorkbenchState) -> Dict[str, Any]:
        task_id = f"sync-{state['content_version_id']}"
        self.db.upsert_delivery_task(task_id, state["content_version_id"], "knowledge_base", "demo-workspace")
        conflict = False
        for case_id in state["verified_case_ids"]:
            row = self.db.connection.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
            case = json.loads(row["evidence_json"])
            key = f"{state['content_version_id']}:{case_id}"
            self.registry.call("knowledge_base", "append_record", idempotency_key=key, record={"case_id": case_id, "title": case["title_original"], "outcome": case["outcome"]["claim"]})
            actual = self.registry.call("knowledge_base", "read_record", record_id=key)
            expected = {"case_id": case_id, "title": case["title_original"], "outcome": case["outcome"]["claim"]}
            if actual != expected:
                conflict = True
                self._event(state, "sync_executor", "readback", "conflict", f"{case_id} 回读内容不一致", expected=expected, actual=actual)
        if conflict:
            task_status, workflow_status = "sync_conflict", "blocked"
            self.db.update_delivery(task_id, task_status, error="回读内容与冻结版本不一致")
        else:
            task_status, workflow_status = "synced", "publishing"
            self.db.update_delivery(task_id, task_status)
            self._event(state, "sync_executor", "readback", "ok", "知识库写入并精确回读通过")
        self.db.update_run(state["run_id"], workflow_status)
        return {"sync_status": task_status}

    @staticmethod
    def route_after_sync(state: WorkbenchState) -> str:
        return "publish" if state.get("sync_status") == "synced" else "end"

    def publish(self, state: WorkbenchState) -> Dict[str, Any]:
        task_id = f"publish-{state['content_version_id']}"
        self.db.upsert_delivery_task(task_id, state["content_version_id"], "demo_channel", "demo-target")
        sent = self.registry.call("distribution", "send_message", task_id=task_id, body_hash=state["content_hash"])
        receipt = self.registry.call("distribution", "get_delivery_receipt", external_id=sent["external_id"])
        status = "unknown" if receipt["status"] == "unknown" else "sent"
        self.db.update_delivery(task_id, status, external_id=sent["external_id"])
        self.db.receipt(task_id, status, json.dumps(receipt, ensure_ascii=False))
        self.db.update_run(state["run_id"], "needs_attention" if status == "unknown" else "completed")
        self._event(state, "publish_executor", "receipt", "pending" if status == "unknown" else "ok", f"发布回执：{status}")
        return {"publish_status": status}

    def recover(self, run_id: str, action: str) -> Any:
        """Retry only the failed external stage from the persisted graph state."""
        config = {"configurable": {"thread_id": run_id}}
        snapshot = self.graph.get_state(config)
        state = dict(snapshot.values)
        if action == "sync":
            if state.get("sync_status") != "sync_conflict":
                raise ValueError("当前运行没有可恢复的同步冲突")
            self.registry.scenario = "happy_path"
            self._event(state, "system", "recovery_started", "ok", "从同步节点局部恢复")
            sync_result = self.sync(state)
            if sync_result.get("sync_status") == "synced":
                publish_result = self.publish({**state, **sync_result})
                return {**state, **sync_result, **publish_result, "recovered": True}
            return {**state, **sync_result, "recovered": False}
        if action == "publish":
            if state.get("publish_status") != "unknown":
                raise ValueError("当前运行没有待查询的未知发布结果")
            task_id = f"publish-{state['content_version_id']}"
            row = self.db.connection.execute("SELECT * FROM delivery_tasks WHERE delivery_task_id = ?", (task_id,)).fetchone()
            if not row or not row["external_id"]:
                raise ValueError("找不到待查询的外部发布请求")
            self.registry.confirm_simulated_receipt(row["external_id"])
            receipt = self.registry.call("distribution", "get_delivery_receipt", external_id=row["external_id"])
            status = "sent" if receipt["status"] == "confirmed" else "unknown"
            self.db.update_delivery(task_id, status, external_id=row["external_id"])
            self.db.receipt(task_id, status, json.dumps(receipt, ensure_ascii=False))
            self.db.update_run(run_id, "completed" if status == "sent" else "needs_attention")
            self._event(state, "publish_executor", "recovery_receipt", "ok" if status == "sent" else "pending", f"查询发布回执：{status}")
            return {**state, "publish_status": status, "recovered": status == "sent"}
        raise ValueError(f"不支持的恢复动作：{action}")

    def start(self, run_id: str, topic: str = "AI 产品案例") -> Any:
        if hasattr(self.registry, "bind_run"):
            self.registry.bind_run(run_id)
        self.db.create_run(run_id, topic, self.scenario, self.connector_mode, self.llm_mode, self.dataset_mode, self.knowledge_mode)
        config = {"configurable": {"thread_id": run_id}}
        return self.graph.invoke({"run_id": run_id, "topic": topic, "scenario": self.scenario}, config=config)

    def resume(self, run_id: str, approval: str = "approve") -> Any:
        config = {"configurable": {"thread_id": run_id}}
        return self.graph.invoke(Command(resume=approval), config=config)


def _has_required_evidence(case: Dict[str, Any]) -> bool:
    required = (case.get("problem", {}), case.get("approach", {}), case.get("outcome", {}))
    return all(item.get("claim") and item.get("evidence") for item in required)


def _safe_editor_item(case: Dict[str, Any]) -> Dict[str, str]:
    return {
        "case_id": case["case_id"],
        "title": case["title_original"],
        "source_url": case["source_url"],
        "problem": case["problem"]["claim"],
        "approach": case["approach"]["claim"],
        "outcome": case["outcome"]["claim"],
    }


def _draft_is_grounded(items: Any, cases: List[Dict[str, Any]]) -> bool:
    if not isinstance(items, list) or len(items) != len(cases):
        return False
    allowed = {
        case["case_id"]: _safe_editor_item(case)
        for case in cases
    }
    for item in items:
        if not isinstance(item, dict) or item.get("case_id") not in allowed:
            return False
        expected = allowed[item["case_id"]]
        if any(item.get(field) != expected[field] for field in expected):
            return False
    return True


def _cluster_id(case: Dict[str, Any]) -> str:
    """Stable local cluster key; a future cluster Agent can replace it."""
    import re
    import hashlib

    title = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(case.get("title_original", "")).lower())
    return "cluster-" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
