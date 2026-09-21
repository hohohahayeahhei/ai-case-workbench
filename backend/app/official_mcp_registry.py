"""Synchronous facade over the official MCP Python SDK stdio client.

LangGraph nodes in this project are synchronous. This adapter starts the
official stdio server for each call and uses a small JSON state file so the
demo remains deterministic across those short-lived server processes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .mock_mcp import ToolSpec


class OfficialMCPRegistry:
    def __init__(self, project_root: str | Path, scenario: str = "happy_path", state_path: str | Path | None = None, fixture_path: str | Path | None = None) -> None:
        self.root = Path(project_root)
        self.scenario = scenario
        self.server_script = self.root / "backend/app/mcp_demo_server.py"
        self.fixture_path = Path(fixture_path or self.root / "data/fixtures/cases.json")
        self.state_path = Path(state_path or "/tmp/ai-case-workbench-mcp-state.json")
        self.name = "official-mcp-stdio"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: Exception | None = None
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._stop_event: asyncio.Event | None = None

    def bind_run(self, run_id: str) -> None:
        if self._thread:
            self.close()
        self.state_path = Path(f"/tmp/ai-case-workbench-mcp-{run_id}.json")

    def _parameters(self) -> StdioServerParameters:
        env = os.environ.copy()
        env["AI_CASE_FIXTURE_PATH"] = str(self.fixture_path)
        env["AI_CASE_MCP_STATE_PATH"] = str(self.state_path)
        env["AI_CASE_MCP_SCENARIO"] = self.scenario
        return StdioServerParameters(command=sys.executable, args=[str(self.server_script)], env=env)

    async def _open(self) -> None:
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(self._parameters()))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

    async def _lifecycle(self) -> None:
        try:
            self._stop_event = asyncio.Event()
            await self._open()
        except Exception as exc:  # surfaced to the calling tool
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        await self._stop_event.wait()
        await self._stack.aclose()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._lifecycle())
        self._loop.run_forever()
        self._loop.close()

    def _ensure_started(self) -> None:
        if self._thread and self._thread.is_alive() and self._session:
            return
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run_loop, name="official-mcp-session", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise TimeoutError("MCP stdio session initialization timed out")
        if self._startup_error:
            raise RuntimeError(f"MCP stdio session failed: {self._startup_error}") from self._startup_error

    async def _invoke(self, tool: str, arguments: Dict[str, Any]) -> Any:
        result = await self._session.call_tool(tool, arguments)
        if result.is_error:
            raise RuntimeError(f"MCP tool failed: {tool}")
        if result.structured_content is not None:
            return result.structured_content.get("result", result.structured_content)
        if result.content and hasattr(result.content[0], "text"):
            return json.loads(result.content[0].text)
        return result.content

    def _call_async(self, tool: str, arguments: Dict[str, Any]) -> Any:
        self._ensure_started()
        future = asyncio.run_coroutine_threadsafe(self._invoke(tool, arguments), self._loop)
        try:
            return future.result(timeout=30)
        except TimeoutError as exc:
            future.cancel()
            raise TimeoutError(f"MCP tool timed out: {tool}") from exc

    def close(self) -> None:
        if not self._loop or not self._thread:
            return
        if self._stop_event and not self._startup_error:
            asyncio.run_coroutine_threadsafe(self._stop(), self._loop).result(timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop = None
        self._thread = None
        self._session = None

    async def _stop(self) -> None:
        self._stop_event.set()

    def call(self, server: str, tool: str, **kwargs: Any) -> Any:
        return self._call_async(tool, kwargs)

    def confirm_simulated_receipt(self, external_id: str) -> None:
        self._call_async("confirm_simulated_receipt", {"external_id": external_id})

    def catalog(self) -> Dict[str, list[Any]]:
        schemas = {
            "search_sources": ("Search demo sources by topic", {"topic": "string", "max_results": "integer"}),
            "search_web": ("Search configured public source candidates", {"query": "string", "max_results": "integer"}),
            "fetch_source": ("Fetch a source fixture", {"url": "string"}),
            "get_source_metadata": ("Read source metadata without changing it", {"url": "string"}),
            "save_evidence_bundle": ("Persist a structured evidence bundle", {"case": "object"}),
            "score_case": ("Compute a versioned explainable case score", {"case": "object"}),
            "append_record": ("Append a frozen record idempotently", {"idempotency_key": "string", "record": "object"}),
            "read_record": ("Read a complete remote record", {"record_id": "string"}),
            "send_message": ("Send a frozen message to a demo channel", {"task_id": "string", "body_hash": "string"}),
            "get_delivery_receipt": ("Query a demo delivery receipt", {"external_id": "string"}),
            "confirm_simulated_receipt": ("Confirm a controlled demo delivery receipt", {"external_id": "string"}),
        }
        return {self.name: [ToolSpec(name, description, schema) for name, (description, schema) in schemas.items()]}
