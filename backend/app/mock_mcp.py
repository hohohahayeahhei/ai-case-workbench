"""In-process MCP-shaped adapters used by the deterministic demo.

The registry mirrors the important MCP idea: named servers expose discoverable
tools and resources. It deliberately has no network or credential dependency;
the adapter boundary can later be replaced with the official MCP SDK client.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Union

from .db import Database, normalize_url
from .scoring import score_case


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any]


class MockMCPServer:
    def __init__(self, name: str) -> None:
        self.name = name
        self.tools: Dict[str, tuple[ToolSpec, Callable[..., Any]]] = {}
        self.resources: Dict[str, Callable[[], Any]] = {}

    def tool(self, name: str, description: str, input_schema: Dict[str, Any]):
        def register(fn: Callable[..., Any]):
            self.tools[name] = (ToolSpec(name, description, input_schema), fn)
            return fn
        return register

    def resource(self, uri: str):
        def register(fn: Callable[[], Any]):
            self.resources[uri] = fn
            return fn
        return register

    def call(self, name: str, **kwargs: Any) -> Any:
        if name not in self.tools:
            raise KeyError(f"Unknown MCP tool: {self.name}.{name}")
        return self.tools[name][1](**kwargs)

    def list_tools(self) -> list[ToolSpec]:
        return [entry[0] for entry in self.tools.values()]


class MockMCPRegistry:
    def __init__(self, fixture_path: Union[str, Path], db: Database, scenario: str = "happy_path") -> None:
        self.db = db
        self.scenario = scenario
        self.servers: Dict[str, MockMCPServer] = {}
        self._cases = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        self._remote_records: Dict[str, Dict[str, Any]] = {}
        self._receipts: Dict[str, str] = {}
        self._evidence_bundles: Dict[str, Dict[str, Any]] = {}
        self._build_servers()

    @staticmethod
    def _public_case(case: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in case.items() if key != "expected_label"}

    def _build_servers(self) -> None:
        source = MockMCPServer("source")

        @source.tool("search_sources", "Search demo sources by topic", {"topic": "string", "max_results": "integer"})
        def search_sources(topic: str, max_results: int = 10) -> list[Dict[str, Any]]:
            needle = topic.strip().lower()
            if not needle:
                matches = self._cases
            else:
                haystacks = [(case, json.dumps(case, ensure_ascii=False).lower()) for case in self._cases]
                tokens = [token for token in needle.split() if token]
                matches = [case for case, haystack in haystacks if any(token in haystack for token in tokens)]
                if not matches:
                    matches = self._cases
            return [self._public_case(case) for case in matches[:max_results]]

        @source.tool("search_web", "Search configured public source candidates", {"query": "string", "max_results": "integer"})
        def search_web(query: str, max_results: int = 10) -> list[Dict[str, Any]]:
            return search_sources(query, max_results)

        @source.tool("fetch_source", "Fetch a source fixture", {"url": "string"})
        def fetch_source(url: str) -> Dict[str, Any]:
            for case in self._cases:
                if normalize_url(case["source_url"]) == normalize_url(url):
                    return self._public_case(case)
            raise KeyError(f"Fixture not found for {url}")

        @source.tool("get_source_metadata", "Read source metadata without changing it", {"url": "string"})
        def get_source_metadata(url: str) -> Dict[str, Any]:
            for case in self._cases:
                if normalize_url(case["source_url"]) == normalize_url(url):
                    return {"url": case["source_url"], "domain": case["source_url"].split("/")[2], "source_type": case.get("source_type", "unknown"), "accessible": True}
            return {"url": url, "domain": url.split("/")[2] if "://" in url else "", "source_type": "unknown", "accessible": False}

        @source.resource("source://catalog")
        def source_catalog() -> list[str]:
            return [case["source_url"] for case in self._cases]

        evidence = MockMCPServer("evidence")

        @evidence.tool("save_evidence_bundle", "Persist a structured evidence bundle", {"case": "object"})
        def save_evidence_bundle(case: Dict[str, Any]) -> Dict[str, str]:
            payload = json.dumps(case, ensure_ascii=False, sort_keys=True).encode("utf-8")
            content_hash = hashlib.sha256(payload).hexdigest()
            self._evidence_bundles[case["case_id"]] = dict(case)
            return {"evidence_id": case["case_id"], "content_hash": content_hash}

        @evidence.tool("get_evidence_bundle", "Read a saved evidence bundle", {"evidence_id": "string"})
        def get_evidence_bundle(evidence_id: str) -> Dict[str, Any]:
            return dict(self._evidence_bundles[evidence_id])

        @evidence.tool("check_duplicate_case", "Check normalized URL and title duplicates", {"url": "string", "title": "string"})
        def check_duplicate_case(url: str, title: str) -> Dict[str, Any]:
            duplicate = any(normalize_url(url) == normalize_url(case["source_url"]) and title == case["title_original"] for case in self._cases[:1])
            return {"duplicate": duplicate, "reason": "normalized URL and title match" if duplicate else "no match"}

        @evidence.tool("score_case", "Compute the versioned explainable case score", {"case": "object"})
        def score_case_tool(case: Dict[str, Any]) -> Dict[str, Any]:
            return score_case(case)

        @evidence.resource("evidence://rules")
        def evidence_rules() -> Dict[str, Any]:
            return {"required_fields": ["problem", "approach", "outcome"], "max_retries": 2}

        knowledge = MockMCPServer("knowledge_base")

        @knowledge.tool("append_record", "Append a frozen record idempotently", {"idempotency_key": "string", "record": "object"})
        def append_record(idempotency_key: str, record: Dict[str, Any]) -> Dict[str, Any]:
            if idempotency_key in self._remote_records:
                return {"status": "already_exists", "record_id": idempotency_key}
            self._remote_records[idempotency_key] = dict(record)
            return {"status": "created", "record_id": idempotency_key}

        @knowledge.tool("read_record", "Read a complete remote record", {"record_id": "string"})
        def read_record(record_id: str) -> Dict[str, Any]:
            record = dict(self._remote_records[record_id])
            if self.scenario == "sync_conflict" and record.get("case_id") == "demo_case_001":
                record["outcome"] = "远端模拟冲突：内容被改变"
            return record

        @knowledge.resource("knowledge://records")
        def list_records() -> Iterable[Dict[str, Any]]:
            return list(self._remote_records.values())

        distribution = MockMCPServer("distribution")

        @distribution.tool("send_message", "Send a frozen message to a demo channel", {"task_id": "string", "body_hash": "string"})
        def send_message(task_id: str, body_hash: str) -> Dict[str, Any]:
            external_id = f"mock-send-{task_id}"
            self._receipts[external_id] = "unknown" if self.scenario == "publish_unknown" else "confirmed"
            return {"status": "unknown" if self.scenario == "publish_unknown" else "accepted", "external_id": external_id, "body_hash": body_hash}

        @distribution.tool("get_delivery_receipt", "Query a demo delivery receipt", {"external_id": "string"})
        def get_delivery_receipt(external_id: str) -> Dict[str, str]:
            return {"status": self._receipts.get(external_id, "unknown")}

        self.servers = {server.name: server for server in (source, evidence, knowledge, distribution)}

    def call(self, server: str, tool: str, **kwargs: Any) -> Any:
        return self.servers[server].call(tool, **kwargs)

    def confirm_simulated_receipt(self, external_id: str) -> None:
        """Resolve an unknown receipt in the demo's controlled simulator."""
        self._receipts[external_id] = "confirmed"

    def catalog(self) -> Dict[str, list[ToolSpec]]:
        return {name: server.list_tools() for name, server in self.servers.items()}
