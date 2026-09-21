"""Tencent Docs MCP knowledge-store adapter.

The public Tencent Docs MCP service is optional. Source/evidence/distribution
tools stay local; only knowledge_base append/read operations go to Tencent
Docs. Tool names and argument templates are configurable because Tencent Docs
offers different tools for document, sheet and smart-canvas targets.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .mock_mcp import MockMCPRegistry, ToolSpec


class TencentDocsRegistry:
    def __init__(self, project_root: str | Path, scenario: str, fixture_path: str | Path) -> None:
        self.local = MockMCPRegistry(fixture_path, _NullDatabase(), scenario)
        self.scenario = scenario
        self.root = Path(project_root)
        self.name = "tencent-docs-mcp"
        self.url = os.environ.get("TENCENT_DOCS_MCP_URL", "https://docs.qq.com/openapi/mcp")
        self.token = os.environ.get("TENCENT_DOCS_TOKEN", "")
        self.write_tool = os.environ.get("TENCENT_DOCS_WRITE_TOOL", "")
        self.read_tool = os.environ.get("TENCENT_DOCS_READ_TOOL", "")
        self.write_template = os.environ.get("TENCENT_DOCS_WRITE_ARGS_JSON", "")
        self.read_template = os.environ.get("TENCENT_DOCS_READ_ARGS_JSON", "")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: Exception | None = None
        self._session: ClientSession | None = None
        self._stack = None
        self._stop_event: asyncio.Event | None = None
        self._tool_names: set[str] = set()

    def _validate_config(self) -> None:
        missing = [name for name, value in {
            "TENCENT_DOCS_TOKEN": self.token,
            "TENCENT_DOCS_WRITE_TOOL": self.write_tool,
            "TENCENT_DOCS_READ_TOOL": self.read_tool,
            "TENCENT_DOCS_WRITE_ARGS_JSON": self.write_template,
            "TENCENT_DOCS_READ_ARGS_JSON": self.read_template,
        }.items() if not value]
        if missing:
            raise RuntimeError("腾讯文档模式缺少配置：" + ", ".join(missing))

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._lifecycle())
        self._loop.run_forever()
        self._loop.close()

    async def _lifecycle(self) -> None:
        try:
            self._validate_config()
            self._stack = __import__("contextlib").AsyncExitStack()
            read, write = await self._stack.enter_async_context(
                streamable_http_client(self.url, headers={"Authorization": self.token})
            )
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            tools = await self._session.list_tools()
            self._tool_names = {tool.name for tool in tools.tools}
        except Exception as exc:
            self._startup_error = exc
        self._ready.set()
        if self._startup_error is None:
            self._stop_event = asyncio.Event()
            await self._stop_event.wait()
            await self._stack.aclose()

    def _ensure_started(self) -> None:
        if self._thread and self._thread.is_alive() and self._session:
            return
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run_loop, name="tencent-docs-mcp-session", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=15):
            raise TimeoutError("腾讯文档 MCP 初始化超时")
        if self._startup_error:
            raise RuntimeError(f"腾讯文档 MCP 连接失败：{self._startup_error}") from self._startup_error

    async def _call_async(self, tool: str, arguments: Dict[str, Any]) -> Any:
        if tool not in self._tool_names:
            raise RuntimeError(f"腾讯文档 MCP 未暴露工具：{tool}；可用工具：{sorted(self._tool_names)}")
        result = await self._session.call_tool(tool, arguments)
        if result.is_error:
            raise RuntimeError(f"腾讯文档工具调用失败：{tool}")
        if result.structured_content is not None:
            return result.structured_content.get("result", result.structured_content)
        if result.content and hasattr(result.content[0], "text"):
            return json.loads(result.content[0].text)
        return result.content

    def _call(self, tool: str, arguments: Dict[str, Any]) -> Any:
        self._ensure_started()
        future = asyncio.run_coroutine_threadsafe(self._call_async(tool, arguments), self._loop)
        return future.result(timeout=45)

    @staticmethod
    def _render(template: str, values: Dict[str, Any]) -> Dict[str, Any]:
        rendered = template
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value)
        return json.loads(rendered)

    def call(self, server: str, tool: str, **kwargs: Any) -> Any:
        if server != "knowledge_base":
            return self.local.call(server, tool, **kwargs)
        if tool == "append_record":
            record = kwargs["record"]
            args = self._render(self.write_template, {"record_id": kwargs["idempotency_key"], "markdown": _record_markdown(record), "record": record})
            result = self._call(self.write_tool, args)
            return {"status": "created", "record_id": kwargs["idempotency_key"], "remote": result}
        if tool == "read_record":
            args = self._render(self.read_template, {"record_id": kwargs["record_id"]})
            result = self._call(self.read_tool, args)
            if isinstance(result, dict) and "record" in result:
                return result["record"]
            raise RuntimeError("腾讯文档读取工具未返回 record 对象；请调整 TENCENT_DOCS_READ_ARGS_JSON 或使用结构化表格工具")
        return self.local.call(server, tool, **kwargs)

    def catalog(self) -> Dict[str, list[ToolSpec]]:
        return {self.name: [ToolSpec("remote_write", "腾讯文档远程写入工具", {}), ToolSpec("remote_read", "腾讯文档远程读取工具", {})]}

    def confirm_simulated_receipt(self, external_id: str) -> None:
        return self.local.confirm_simulated_receipt(external_id)

    def close(self) -> None:
        if not self._loop or not self._thread:
            return
        if self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop = None
        self._thread = None
        self._session = None


class _NullDatabase:
    def event(self, *args: Any, **kwargs: Any) -> None:
        return None


def _record_markdown(record: Dict[str, Any]) -> str:
    return "\n".join([f"## {record.get('title', record.get('case_id', 'case'))}", f"- case_id: {record.get('case_id', '')}", f"- outcome: {record.get('outcome', '')}"]) + "\n"
