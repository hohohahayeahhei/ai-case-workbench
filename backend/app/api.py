"""FastAPI surface for the local AI Case Intelligence Workbench."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from pathlib import Path
from typing import Any, Dict
from concurrent.futures import ThreadPoolExecutor
import fcntl
import threading

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.langgraph_workflow import LangGraphWorkbench
from backend.app.evaluation import UnsafeModel, evaluate_fixture
from backend.app.exporter import build_markdown_report
from backend.app.query_agent import QueryAgent
from backend.app.discovery_agent import SourceDiscoveryAgent
from backend.app.collection_pipeline import CollectionPipeline
from backend.app.collection_export import export_collection, build_report, tencent_rows
from backend.app.config import runtime_dir
from backend.app.collection_health import item_diagnostic, retry_ready
from backend.app.publication_dates import temporal_view, date_policy
from backend.app.evidence_translation import EvidenceTranslator
from backend.app.douyin_api import create_douyin_router


# Runtime databases live in the OS temp directory so the demo also works in
# restricted desktop sandboxes where the project folder is read-only at runtime.
DATA_DIR = runtime_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="AI Case Intelligence Workbench API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ProblemError(Exception):
    def __init__(self, code: str, detail: str, status: int = 400) -> None:
        self.code = code
        self.detail = detail
        self.status = status


@app.exception_handler(ProblemError)
async def problem_error_handler(_, exc: ProblemError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={
            "type": f"/problems/{exc.code}",
            "title": exc.code.replace("_", " ").title(),
            "status": exc.status,
            "detail": exc.detail,
            "code": exc.code,
            "requestId": uuid4().hex[:12],
        },
        media_type="application/problem+json",
    )

workbenches: Dict[str, LangGraphWorkbench] = {}
discovery_agent = SourceDiscoveryAgent(ROOT)
CATALOG_DB_PATH = DATA_DIR / "catalog.db"
query_agent = QueryAgent(ROOT, CATALOG_DB_PATH)
collection_pipeline = CollectionPipeline(ROOT, CATALOG_DB_PATH)
collection_workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="case-collection")
collection_start_lock = threading.Lock()
collection_futures = {}


def recover_abandoned_collections() -> None:
    with open(str(CATALOG_DB_PATH) + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # A CLI collector still owns these running records.
        for result in collection_pipeline.db.collection_history(100):
            if result.get("status") in {"running", "queued"}:
                result.update(status="interrupted", error="服务中断；已保存的候选可通过重试待处理继续")
                collection_pipeline.db.save_collection_run(result)


recover_abandoned_collections()


class StartRequest(BaseModel):
    topic: str = Field(default="AI 产品案例", min_length=1, max_length=100)
    scenario: str = Field(default="happy_path")
    llm_mode: str = Field(default="mock")
    connector_mode: str = Field(default="mock")
    dataset_mode: str = Field(default="fixture")
    knowledge_mode: str = Field(default="local_sqlite")


class ReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")


class RecoveryRequest(BaseModel):
    action: str = Field(pattern="^(sync|publish)$")


class FeedDiscoveryRequest(BaseModel):
    feed_urls: list[str] = Field(min_length=1, max_length=5)
    query: str = Field(default="", max_length=100)
    max_results: int = Field(default=20, ge=1, le=100)


class CollectionRunRequest(BaseModel):
    source_mode: str = Field(default="live", pattern="^(live|online_snapshot|fixture|all)$")
    query: str = Field(default="", max_length=500)
    feed_urls: list[str] = Field(default_factory=list, max_length=5)
    include_catalog: bool = False
    max_results: int = Field(default=12, ge=1, le=20)
    search_web: bool = True
    retry_pending: bool = False
    force: bool = False


class EvidenceTranslationRequest(BaseModel):
    quotes: list[str] = Field(min_length=1, max_length=8)


@app.post('/api/v1/collection/items/{case_id}/evidence-translation')
def translate_evidence(case_id: str, request: EvidenceTranslationRequest):
    row = collection_pipeline.db.source_item(case_id)
    if not row:
        raise HTTPException(404, '找不到该案例')
    try:
        return EvidenceTranslator(DATA_DIR / 'evidence-translations').translate(json.loads(row['payload_json']), request.quotes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except (RuntimeError, OSError, KeyError, TypeError):
        raise HTTPException(503, '中文翻译暂时不可用，请稍后重试；原文证据未改变。') from None


def get_workbench(run_id: str) -> LangGraphWorkbench:
    if run_id not in workbenches:
        if not re.fullmatch(r"ui-[a-f0-9]{10}", run_id):
            raise HTTPException(404, "找不到该运行。")
        business_path = DATA_DIR / f"ui-{run_id}-business.db"
        checkpoint_path = DATA_DIR / f"ui-{run_id}-checkpoints.db"
        if not business_path.exists() or not checkpoint_path.exists():
            raise HTTPException(404, "找不到该运行。请确认服务进程仍在运行，或重新开始演示。")
        connection = sqlite3.connect(business_path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute("SELECT scenario, connector_mode, llm_mode, dataset_mode, knowledge_mode FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        except sqlite3.OperationalError:
            row = connection.execute("SELECT scenario, connector_mode, llm_mode, dataset_mode FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        connection.close()
        if not row:
            raise HTTPException(404, "找不到该运行。")
        workbenches[run_id] = LangGraphWorkbench(
            ROOT,
            str(business_path),
            str(checkpoint_path),
            row["scenario"],
            connector_mode=row["connector_mode"] if "connector_mode" in row.keys() else "mock",
            llm_mode=row["llm_mode"] if "llm_mode" in row.keys() else "mock",
            run_id=run_id,
            dataset_mode=row["dataset_mode"] if "dataset_mode" in row.keys() else "fixture",
            knowledge_mode=row["knowledge_mode"] if "knowledge_mode" in row.keys() else "local_sqlite",
        )
    return workbenches[run_id]


def row_dict(row: Any) -> Dict[str, Any]:
    return dict(row) if row else {}


def serialize_run(workbench: LangGraphWorkbench, run_id: str, graph_result: Any | None = None) -> Dict[str, Any]:
    run = workbench.db.run(run_id)
    if not run:
        raise HTTPException(404, "找不到该运行")
    version = workbench.db.content_version(run_id)
    cases = []
    for row in workbench.db.cases(run_id):
        item = row_dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        item["scorecard"] = json.loads(item.pop("scorecard_json", "{}"))
        cases.append(item)
    events = []
    for row in workbench.db.events(run_id):
        item = row_dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        events.append(item)
    result = {
        "run": row_dict(run),
        "counts": workbench.db.counts(run_id),
        "cases": cases,
        "content_version": row_dict(version),
        "deliveries": [row_dict(row) for row in workbench.db.deliveries(run_id)],
        "events": events,
        "interrupted": bool(
            (graph_result and "__interrupt__" in graph_result)
            or run["status"] == "awaiting_review"
        ),
    }
    if graph_result and "__interrupt__" in graph_result:
        result["review_request"] = str(graph_result["__interrupt__"][0])
    return result


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "mode": "local-demo"}


@app.get("/api/config")
def public_config() -> Dict[str, Any]:
    """Expose safe runtime capabilities so the UI can select configured modes."""
    return {
        "llm_mode": os.environ.get("AI_CASE_LLM_MODE", "mock"),
        "llm_model": os.environ.get("AI_CASE_LLM_MODEL", ""),
        "llm_configured": bool(os.environ.get("OPENAI_API_KEY")),
        "tencent_docs_configured": bool(os.environ.get("TENCENT_DOCS_TOKEN")),
    }


@app.get("/api/readyz")
def ready() -> Dict[str, Any]:
    fixture = ROOT / "data/fixtures/cases.json"
    server = ROOT / "backend/app/mcp_demo_server.py"
    checks = {"fixture": fixture.exists(), "mcp_server": server.exists(), "runtime_dir": DATA_DIR.exists()}
    return {"status": "ready" if all(checks.values()) else "not_ready", "checks": checks}


def _query_error(exc: ValueError) -> ProblemError:
    code = "invalid_cursor" if "cursor" in str(exc) else "invalid_request"
    return ProblemError(code, str(exc))


@app.get("/api/v1/cases")
def query_cases(
    query: str = Query(default="", max_length=100),
    window: str = Query(default="7d", pattern="^(24h|7d|30d|1y|all)$"),
    category: str | None = Query(default=None, max_length=50),
    source_type: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=100),
    sort: str = Query(default="relevance", pattern="^(relevance|latest)$"),
    source_mode: str = Query(default="online_snapshot", pattern="^(live|online_snapshot|fixture|all)$"),
    mode: str = Query(default="selected", pattern="^(selected|all)$"),
) -> Dict[str, Any]:
    """Stable read-only query contract for the AI Case Intelligence Skill."""
    try:
        return query_agent.query(query, window, category, source_type, limit, cursor, sort, source_mode, mode)
    except ValueError as exc:
        raise _query_error(exc) from exc


@app.get("/api/v1/hot")
def hot_cases(
    limit: int = Query(default=10, ge=1, le=100),
    source_mode: str = Query(default="online_snapshot", pattern="^(live|online_snapshot|fixture|all)$"),
) -> Dict[str, Any]:
    try:
        return query_agent.hot(limit, source_mode)
    except ValueError as exc:
        raise _query_error(exc) from exc


@app.get("/api/v1/digests/latest")
def latest_digest(
    window: str = Query(default="7d", pattern="^(24h|7d|30d|1y|all)$"),
    limit: int = Query(default=10, ge=1, le=100),
    source_mode: str = Query(default="online_snapshot", pattern="^(live|online_snapshot|fixture|all)$"),
) -> Dict[str, Any]:
    try:
        return query_agent.digest(window, limit, source_mode)
    except ValueError as exc:
        raise _query_error(exc) from exc


@app.get("/api/v1/snapshot")
def query_snapshot(
    cursor: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
    source_mode: str = Query(default="online_snapshot", pattern="^(live|online_snapshot|fixture|all)$"),
) -> Dict[str, Any]:
    try:
        return query_agent.snapshot(cursor, limit, source_mode)
    except ValueError as exc:
        raise _query_error(exc) from exc


@app.get("/api/v1/selected/snapshot")
def selected_snapshot(
    page: str | None = Query(default=None, max_length=500),
    limit: int = Query(default=50, ge=1, le=1000),
    source_mode: str = Query(default="online_snapshot", pattern="^(live|online_snapshot|fixture|all)$"),
) -> Dict[str, Any]:
    try:
        return query_agent.selected_snapshot(page, limit, source_mode)
    except ValueError as exc:
        raise _query_error(exc) from exc


@app.get("/api/v1/selected/changes")
def selected_changes(
    cursor: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(default=100, ge=1, le=1000),
    source_mode: str = Query(default="online_snapshot", pattern="^(live|online_snapshot|fixture|all)$"),
) -> Dict[str, Any]:
    try:
        return query_agent.selected_changes(cursor, limit, source_mode)
    except ValueError as exc:
        raise _query_error(exc) from exc


@app.get("/api/v1/health")
def query_health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "contract_version": query_agent.contract_version,
        "agent": query_agent.name,
        "read_only": True,
        "data_status": "curated_snapshot",
    }


@app.get("/api/v1/discovery/preview")
def discovery_preview(
    query: str = Query(default="", max_length=100),
    source_mode: str = Query(default="online_snapshot", pattern="^(live|online_snapshot|fixture|all)$"),
    limit: int = Query(default=20, ge=1, le=100),
) -> Dict[str, Any]:
    """Return deterministic candidates before extraction and verification."""
    if source_mode not in {"online_snapshot", "fixture", "all"}:
        raise _query_error(ValueError("unsupported source mode"))
    candidates = discovery_agent.fixture_candidates(source_mode)
    if query:
        tokens = [token.lower() for token in query.split() if token]
        candidates = [
            item for item in candidates
            if any(token in json.dumps(item, ensure_ascii=False).lower() for token in tokens)
        ]
    return {
        "agent": discovery_agent.name,
        "stage": "candidate_discovery",
        "read_only": True,
        "items": candidates[:limit],
        "next_stage": "extract -> verify -> score -> curate",
    }


@app.get("/api/v1/sources")
def source_catalog() -> Dict[str, Any]:
    catalog_path = ROOT / "data/fixtures/source_catalog.json"
    return {"items": json.loads(catalog_path.read_text(encoding="utf-8")), "read_only": True}


@app.post("/api/v1/collection/runs")
def run_collection(request: CollectionRunRequest) -> Dict[str, Any]:
    try:
        result = collection_pipeline.run(**request.model_dump(exclude={"force"}))
        result["artifacts"] = export_collection(collection_pipeline.db, result, DATA_DIR / "exports")
        collection_pipeline.db.save_collection_run(result)
        return result
    except (OSError, ValueError) as exc:
        raise _query_error(ValueError(f"collection failed: {exc}")) from exc


def _collect_in_background(run_id: str, request: CollectionRunRequest) -> None:
    pipeline = CollectionPipeline(ROOT, CATALOG_DB_PATH)
    try:
        result = pipeline.run(**request.model_dump(exclude={"force"}), run_id=run_id)
        result["artifacts"] = export_collection(pipeline.db, result, DATA_DIR / "exports")
        pipeline.db.save_collection_run(result)
    except Exception as exc:
        result = pipeline.db.collection_run(run_id) or {"run_id": run_id}
        result.update(status="failed", error=pipeline._error(exc))
        pipeline.db.save_collection_run(result)
    finally:
        pipeline.close()


@app.post("/api/v1/collection/jobs", status_code=202)
def start_collection_job(request: CollectionRunRequest) -> Dict[str, Any]:
    # Check-and-enqueue is one operation even when several tabs submit together.
    with collection_start_lock:
        return _enqueue_collection_job(request)


def _enqueue_collection_job(request: CollectionRunRequest) -> Dict[str, Any]:
    for run_id, future in list(collection_futures.items()):
        if future.done():
            collection_futures.pop(run_id, None)
    # The file lock in CollectionPipeline serializes this with CLI collection.
    if request.source_mode == "live":
        existing = _active_live_collection()
        if existing:
            return existing
        if not request.force:
            recent = _recent_live_collection()
            if recent:
                return recent
    result = {"run_id": "collect-" + uuid4().hex[:12], "status": "queued", "source_mode": request.source_mode,
              "query": request.query, "started_at": datetime.now(timezone.utc).isoformat(), "processed_count": 0, "events": []}
    collection_pipeline.db.save_collection_run(result)
    collection_futures[result['run_id']] = collection_workers.submit(_collect_in_background, result["run_id"], request)
    return result


def _active_live_collection() -> Dict[str, Any] | None:
    """Reuse a live job already running; recover abandoned jobs before starting another."""
    now = datetime.now(timezone.utc)
    for result in collection_pipeline.db.collection_history(30):
        if result.get("source_mode") != "live" or result.get("status") not in {"queued", "running"}:
            continue
        future = collection_futures.get(result['run_id'])
        if future is not None and not future.done():
            return result
        timestamps = [result.get("started_at")]
        timestamps.extend(event.get("at") for event in result.get("events", [])[-3:])
        last_seen = None
        for value in timestamps:
            if not value:
                continue
            try:
                last_seen = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
        if last_seen and now - last_seen > timedelta(minutes=15):
            # A sleeping computer or slow external request can leave a long gap.
            # An event timestamp alone cannot prove that the worker has died.
            with open(str(CATALOG_DB_PATH) + '.lock', 'a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return result
                result.update(status="interrupted", error="采集进程已退出；已保存的候选可重试", finished_at=now.isoformat())
                collection_pipeline.db.save_collection_run(result)
            continue
        return result
    return None


def _recent_live_collection() -> Dict[str, Any] | None:
    """Avoid starting the same expensive live scan from multiple browser tabs."""
    now = datetime.now(timezone.utc)
    for result in collection_pipeline.db.collection_history(30):
        if result.get("source_mode") != "live" or result.get("status") not in {"completed", "partial"}:
            continue
        value = result.get("finished_at") or result.get("started_at")
        if not value:
            continue
        try:
            finished = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if now - finished <= timedelta(minutes=30):
            return result
    return None


def _review_douyin_in_background(run_id: str, case_id: str) -> None:
    pipeline = CollectionPipeline(ROOT, CATALOG_DB_PATH)
    try:
        result = pipeline.run(source_mode='live', search_web=False, retry_pending=True,
                              retry_case_ids=[case_id], max_results=1, run_id=run_id)
        result['artifacts'] = export_collection(pipeline.db, result, DATA_DIR / 'exports')
        pipeline.db.save_collection_run(result)
    except Exception as exc:
        result = pipeline.db.collection_run(run_id) or {'run_id': run_id}
        result.update(status='failed', error=pipeline._error(exc))
        pipeline.db.save_collection_run(result)
    finally:
        pipeline.close()


def _submit_douyin_review(case_id: str) -> Dict[str, Any]:
    with collection_start_lock:
        if _active_live_collection():
            raise HTTPException(409, '已有采集运行，请待当前运行完成后提交此作品')
        result = {'run_id': 'collect-' + uuid4().hex[:12], 'status': 'queued', 'source_mode': 'live',
                  'query': '', 'started_at': datetime.now(timezone.utc).isoformat(),
                  'processed_count': 0, 'events': [], 'retry_case_ids': [case_id]}
        collection_pipeline.db.save_collection_run(result)
        collection_futures[result['run_id']] = collection_workers.submit(_review_douyin_in_background, result['run_id'], case_id)
        return result


app.include_router(create_douyin_router(collection_pipeline.douyin, collection_pipeline, _submit_douyin_review))


@app.get("/api/v1/collection/jobs")
def collection_history() -> Dict[str, Any]:
    return {"items": collection_pipeline.db.collection_history()}


@app.get("/api/v1/collection/jobs/{run_id}")
def collection_job(run_id: str) -> Dict[str, Any]:
    result = collection_pipeline.db.collection_run(run_id)
    if result is None:
        raise HTTPException(404, "找不到采集运行")
    return result


@app.get("/api/v1/collection/jobs/{run_id}/report", response_class=PlainTextResponse)
def collection_report(run_id: str) -> str:
    result = collection_job(run_id)
    path = DATA_DIR / "exports" / f"{run_id}.md"
    return path.read_text(encoding="utf-8") if path.is_file() else build_report(collection_pipeline.db, result)


@app.get("/api/v1/collection/tencent-preview")
def tencent_preview() -> Dict[str, Any]:
    return {"delivery_status": "stored_locally", "canonical_store": "local_sqlite",
            "remote_export_status": "optional", "records": tencent_rows(collection_pipeline.db)}


@app.get("/api/v1/collection/items")
def collection_items(
    status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
) -> Dict[str, Any]:
    # Apply temporal status before filtering/limiting so stale approvals cannot leak.
    rows = collection_pipeline.db.source_items(limit=10000)
    items = []
    for row in rows:
        item = json.loads(row["payload_json"])
        item.update({"status": row["status"], "discovered_at": row["discovered_at"], "source_item_id": row["source_item_id"]})
        if item.get("scorecard"):
            from .scoring import score_case
            item["scorecard"] = score_case(item)
            item["status"] = "selected" if item["scorecard"]["selected"] else item["scorecard"]["tier"]
        source_meta = collection_pipeline.discovery.source_metadata(item.get("source_url", ""))
        item.update({key: value for key, value in source_meta.items() if value is not None})
        item = temporal_view(item)
        item['diagnostic'] = item_diagnostic(item)
        item['can_retry'] = item['status'] in {'needs_extraction', 'needs_scoring', 'needs_date'} and retry_ready(item)
        if status is None or item['status'] == status:
            items.append(item)
    total = len(items)
    return {"items": items[:limit], "count": len(items[:limit]), "total": total, "read_only": True, "date_policy": date_policy()}


@app.get("/api/v1/collection/clusters")
def collection_clusters(limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
    items = []
    for row in collection_pipeline.db.clusters(limit):
        item = dict(row)
        item["item_ids"] = json.loads(item.pop("item_ids_json"))
        items.append(item)
    return {"items": items, "count": len(items), "read_only": True}


@app.get("/api/v1/collection/documents")
def collection_documents(limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
    items = [dict(row) for row in collection_pipeline.db.raw_documents(limit)]
    return {"items": items, "count": len(items), "read_only": True}


@app.post("/api/v1/discovery/feeds")
def discover_feeds(request: FeedDiscoveryRequest) -> Dict[str, Any]:
    """Fetch explicitly supplied RSS/Atom feeds as untrusted candidates."""
    try:
        items = discovery_agent.discover_feeds(request.feed_urls, request.query, request.max_results)
        return {
            "agent": discovery_agent.name,
            "stage": "candidate_discovery",
            "read_only": True,
            "items": items,
            "next_stage": "fetch article -> extract evidence -> verify -> score",
        }
    except (OSError, ValueError) as exc:
        raise _query_error(ValueError(f"feed discovery failed: {exc}")) from exc


@app.get("/api/tools")
def tools_catalog() -> Dict[str, Any]:
    workbench = next(iter(workbenches.values()), None)
    if workbench is None:
        workbench = LangGraphWorkbench(ROOT, ":memory:", ":memory:")
        temporary = True
    else:
        temporary = False
    try:
        return {
            "mode": workbench.connector_mode,
            "servers": {
                "Live Source（真实网络）": [
                    {"name": "search_web", "description": "Responses 原生搜索，经真实 MCP stdio 调用；仅接收工具来源引用", "input_schema": {"query": "string"}},
                    {"name": "fetch_page", "description": "抓取公开原文，限制体积并保存哈希；拒绝访问会保留原因", "input_schema": {"url": "string"}},
                    {"name": "score_case", "description": "计算版本化的六维评分，证据硬门槛优先", "input_schema": {"case": "object"}},
                ],
                **{
                name: [
                    {"name": spec.name, "description": spec.description, "input_schema": spec.input_schema}
                    for spec in specs
                ]
                for name, specs in workbench.registry.catalog().items()
                },
            },
        }
    finally:
        if temporary:
            workbench.close()


@app.post("/api/runs")
def start_run(request: StartRequest) -> Dict[str, Any]:
    if request.scenario not in {"happy_path", "sync_conflict", "publish_unknown"}:
        raise HTTPException(400, "不支持的演示场景")
    if request.llm_mode not in {"mock", "openai_compatible"}:
        raise HTTPException(400, "不支持的模型模式")
    if request.connector_mode not in {"mock", "official_mcp"}:
        raise HTTPException(400, "不支持的连接器模式")
    if request.dataset_mode not in {"fixture", "online_snapshot"}:
        raise HTTPException(400, "不支持的数据集模式")
    if request.knowledge_mode not in {"local_sqlite", "tencent_docs"}:
        raise HTTPException(400, "不支持的知识库模式")
    run_id = f"ui-{uuid4().hex[:10]}"
    workbench = LangGraphWorkbench(
        ROOT,
        str(DATA_DIR / f"ui-{run_id}-business.db"),
        str(DATA_DIR / f"ui-{run_id}-checkpoints.db"),
        request.scenario,
        model=__import__("backend.app.agent_model", fromlist=["create_agent_model"]).create_agent_model(request.llm_mode),
        connector_mode=request.connector_mode,
        llm_mode=request.llm_mode,
        run_id=run_id,
        dataset_mode=request.dataset_mode,
        knowledge_mode=request.knowledge_mode,
    )
    workbenches[run_id] = workbench
    try:
        result = workbench.start(run_id, request.topic)
        return serialize_run(workbench, run_id, result)
    except Exception as exc:
        workbench.db.update_run(run_id, "failed")
        workbench.db.event(run_id, "system", "run_failed", "error", f"运行失败：{exc}")
        return serialize_run(workbench, run_id)


@app.get("/api/runs")
def list_runs(limit: int = Query(default=20, ge=1, le=100)) -> Dict[str, Any]:
    """List persisted runs without reopening every LangGraph checkpointer."""
    items = []
    for path in DATA_DIR.glob("ui-*-business.db"):
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        for row in rows:
            item = dict(row)
            item["case_counts"] = {}
            count_rows = connection.execute("SELECT status, COUNT(*) AS count FROM cases WHERE run_id = ? GROUP BY status", (item["run_id"],)).fetchall()
            item["case_counts"] = {count["status"]: count["count"] for count in count_rows}
            last_event = connection.execute("SELECT actor, message, created_at FROM run_events WHERE run_id = ? ORDER BY event_id DESC LIMIT 1", (item["run_id"],)).fetchone()
            item["last_event"] = dict(last_event) if last_event else None
            items.append(item)
        connection.close()
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return {"runs": items[:limit]}


@app.post("/api/runs/{run_id}/review")
def review_run(run_id: str, request: ReviewRequest) -> Dict[str, Any]:
    workbench = get_workbench(run_id)
    try:
        result = workbench.resume(run_id, request.decision)
        return serialize_run(workbench, run_id, result)
    except Exception as exc:
        workbench.db.update_run(run_id, "failed")
        workbench.db.event(run_id, "system", "run_failed", "error", f"审核后执行失败：{exc}")
        return serialize_run(workbench, run_id)
        raise HTTPException(500, f"恢复运行失败：{exc}") from exc


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> Dict[str, Any]:
    return serialize_run(get_workbench(run_id), run_id)


@app.get("/api/runs/{run_id}/export")
def export_run(run_id: str, format: str = Query(default="markdown")) -> PlainTextResponse:
    if format != "markdown":
        raise HTTPException(400, "目前只支持 markdown 导出")
    report = build_markdown_report(serialize_run(get_workbench(run_id), run_id))
    return PlainTextResponse(
        report,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{run_id}-report.md"'},
    )


@app.post("/api/runs/{run_id}/recover")
def recover_run(run_id: str, request: RecoveryRequest) -> Dict[str, Any]:
    workbench = get_workbench(run_id)
    try:
        result = workbench.recover(run_id, request.action)
        return serialize_run(workbench, run_id, result)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        workbench.db.update_run(run_id, "failed")
        workbench.db.event(run_id, "system", "run_failed", "error", f"恢复失败：{exc}")
        return serialize_run(workbench, run_id)


@app.get("/api/evaluations/latest")
def latest_evaluation() -> Dict[str, Any]:
    fixture = ROOT / "data/fixtures/cases.json"
    return {
        "dataset": evaluate_fixture(fixture),
        "adversarial": evaluate_fixture(fixture, UnsafeModel()),
    }
