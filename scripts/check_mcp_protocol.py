"""Protocol-level smoke test for the real official MCP stdio server."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "backend/app/mcp_demo_server.py"


async def check() -> None:
    env = os.environ.copy()
    env["AI_CASE_FIXTURE_PATH"] = str(ROOT / "data/fixtures/cases.json")
    state_directory = tempfile.TemporaryDirectory(prefix="ai-case-mcp-check-")
    env["AI_CASE_MCP_STATE_PATH"] = str(Path(state_directory.name) / "state.json")
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            required = {
                "search_sources", "fetch_source", "save_evidence_bundle",
                "check_duplicate_case", "append_record", "read_record",
                "send_message", "get_delivery_receipt",
            }
            assert required.issubset(tool_names), tool_names

            search = await session.call_tool("search_sources", {"topic": "AI 产品案例", "max_results": 3})
            assert len(search.structured_content["result"]) == 3
            scored = await session.call_tool("score_case", {"case": search.structured_content["result"][0]})
            assert not scored.is_error
            assert "quality_score" in scored.structured_content.get("result", scored.structured_content)

            duplicate = await session.call_tool(
                "check_duplicate_case",
                {"url": "https://example.com/cases/customer-support-ai?utm_source=feed", "title": "示例：客服团队用 AI 助手缩短知识检索时间"},
            )
            assert duplicate.structured_content["result"]["duplicate"] is True

            created = await session.call_tool("append_record", {"idempotency_key": "protocol-demo-1", "record": {"case_id": "demo_case_001"}})
            repeated = await session.call_tool("append_record", {"idempotency_key": "protocol-demo-1", "record": {"case_id": "changed"}})
            assert created.structured_content["result"]["status"] == "created"
            assert repeated.structured_content["result"]["status"] == "already_exists"

            resources = await session.list_resources()
            assert any(str(resource.uri) == "catalog://cases" for resource in resources.resources)
            prompts = await session.list_prompts()
            assert any(prompt.name == "verification_prompt" for prompt in prompts.prompts)

            print(json.dumps({
                "tools": len(tools.tools),
                "resource": "catalog://cases",
                "prompt": "verification_prompt",
                "idempotency": "verified",
            }, ensure_ascii=False))
    state_directory.cleanup()
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "backend/app/mcp_live_server.py")], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            assert {"search_web", "fetch_page", "score_case"} <= names
            result = await session.call_tool("score_case", {"case": {}})
            assert not result.is_error
            assert not result.structured_content.get("result", result.structured_content)["selected"]
            print("Live MCP schemas and score_case verified (offline)")


if __name__ == "__main__":
    asyncio.run(check())
