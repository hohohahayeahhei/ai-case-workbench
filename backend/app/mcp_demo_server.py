"""Real MCP stdio server backed only by the project's redacted fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.scoring import score_case as compute_score_case


FIXTURE_PATH = Path(os.environ.get("AI_CASE_FIXTURE_PATH", "data/fixtures/cases.json"))
STATE_PATH = Path(os.environ.get("AI_CASE_MCP_STATE_PATH", "/tmp/ai-case-workbench-mcp-state.json"))
CASES = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def public_case(case: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in case.items() if key != "expected_label"}
if STATE_PATH.exists():
    _state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
else:
    _state = {"remote_records": {}, "receipts": {}}
REMOTE_RECORDS: Dict[str, Dict[str, Any]] = _state.get("remote_records", {})
RECEIPTS: Dict[str, str] = _state.get("receipts", {})


def persist_state() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"remote_records": REMOTE_RECORDS, "receipts": RECEIPTS}, ensure_ascii=False), encoding="utf-8")

mcp = MCPServer("AI Case Workbench Demo", version="0.1.0")


def normalize_url(url: str) -> str:
    value = url.strip().lower().split("#", 1)[0]
    if "?" in value:
        base, query = value.split("?", 1)
        parts = [part for part in query.split("&") if not part.startswith("utm_")]
        value = base + (("?" + "&".join(parts)) if parts else "")
    return value.rstrip("/")


@mcp.tool()
def search_sources(topic: str, max_results: int = 10) -> list[Dict[str, Any]]:
    """Search the redacted case catalog for a topic."""
    needle = topic.strip().lower()
    if not needle:
        matches = CASES
    else:
        haystacks = [(case, json.dumps(case, ensure_ascii=False).lower()) for case in CASES]
        tokens = [token for token in needle.split() if token]
        matches = [case for case, haystack in haystacks if any(token in haystack for token in tokens)]
        if not matches:
            matches = CASES
    return [public_case(case) for case in matches[:max_results]]


@mcp.tool()
def search_web(query: str, max_results: int = 10) -> list[Dict[str, Any]]:
    """Search OFFLINE demo fixtures only; use mcp_live_server for the public web."""
    return search_sources(query, max_results)


@mcp.tool()
def fetch_source(url: str) -> Dict[str, Any]:
    """Read one source fixture by URL."""
    for case in CASES:
        if normalize_url(case["source_url"]) == normalize_url(url):
            return public_case(case)
    raise ValueError(f"source not found: {url}")


@mcp.tool()
def get_source_metadata(url: str) -> Dict[str, Any]:
    """Read source metadata without changing it."""
    for case in CASES:
        if normalize_url(case["source_url"]) == normalize_url(url):
            return {"url": case["source_url"], "domain": urlparse(case["source_url"]).netloc, "source_type": case.get("source_type", "unknown"), "accessible": True}
    return {"url": url, "domain": urlparse(url).netloc, "source_type": "unknown", "accessible": False}


@mcp.tool()
def save_evidence_bundle(case: Dict[str, Any]) -> Dict[str, str]:
    """Create a deterministic evidence bundle hash."""
    payload = json.dumps(case, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {"evidence_id": case["case_id"], "content_hash": hashlib.sha256(payload).hexdigest()}


@mcp.tool()
def score_case(case: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the project's versioned explainable scorecard."""
    return compute_score_case(case)


@mcp.tool()
def check_duplicate_case(url: str, title: str) -> Dict[str, Any]:
    """Check whether a URL/title matches an existing fixture."""
    matches = [
        case["case_id"] for case in CASES
        if normalize_url(case["source_url"]) == normalize_url(url) and case["title_original"] == title
    ]
    return {"duplicate": bool(matches), "matching_case_ids": matches}


@mcp.tool()
def append_record(idempotency_key: str, record: Dict[str, Any]) -> Dict[str, str]:
    """Append a record once using an idempotency key."""
    if idempotency_key in REMOTE_RECORDS:
        return {"status": "already_exists", "record_id": idempotency_key}
    REMOTE_RECORDS[idempotency_key] = record
    persist_state()
    return {"status": "created", "record_id": idempotency_key}


@mcp.tool()
def read_record(record_id: str) -> Dict[str, Any]:
    """Read a complete simulated knowledge-base record."""
    if record_id not in REMOTE_RECORDS:
        raise ValueError(f"record not found: {record_id}")
    record = dict(REMOTE_RECORDS[record_id])
    if os.environ.get("AI_CASE_MCP_SCENARIO") == "sync_conflict" and record.get("case_id") == "demo_case_001":
        record["outcome"] = "远端模拟冲突：内容被改变"
    return record


@mcp.tool()
def send_message(task_id: str, body_hash: str) -> Dict[str, str]:
    """Accept a frozen message and create a simulated delivery receipt."""
    external_id = f"mock-send-{task_id}"
    RECEIPTS[external_id] = "confirmed"
    if os.environ.get("AI_CASE_MCP_SCENARIO") == "publish_unknown":
        RECEIPTS[external_id] = "unknown"
    persist_state()
    return {"status": "accepted", "external_id": external_id, "body_hash": body_hash}


@mcp.tool()
def get_delivery_receipt(external_id: str) -> Dict[str, str]:
    """Read a simulated delivery receipt."""
    return {"status": RECEIPTS.get(external_id, "unknown")}


@mcp.tool()
def confirm_simulated_receipt(external_id: str) -> Dict[str, str]:
    """Resolve a pending receipt in the controlled demo simulator."""
    RECEIPTS[external_id] = "confirmed"
    persist_state()
    return {"status": "confirmed", "external_id": external_id}


@mcp.resource("catalog://cases")
def case_catalog() -> str:
    """Expose the redacted case catalog as an MCP resource."""
    return json.dumps([public_case(case) for case in CASES], ensure_ascii=False)


@mcp.prompt()
def verification_prompt(case_id: str) -> str:
    """Return the verification rubric as an MCP prompt."""
    return (
        f"核验案例 {case_id}：检查问题、方法、结果是否都有原文证据；"
        "数字是否有出处；是否与已有案例重复；输出通过、补证或淘汰及理由。"
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
