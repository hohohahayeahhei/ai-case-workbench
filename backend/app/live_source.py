"""Actual Search/Fetch MCP client; no fixture fallback."""
import asyncio
import os
import sys
from pathlib import Path
from .network_policy import mcp_timeout

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class LiveSourceClient:
    def __init__(self, root: Path):
        self.root = root

    def call(self, tool: str, **arguments):
        async def invoke():
            params = StdioServerParameters(command=sys.executable,
                args=[str(self.root / "backend/app/mcp_live_server.py")], env=os.environ.copy())
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=mcp_timeout(tool)) as session:
                    await session.initialize()
                    response = await session.call_tool(tool, arguments)
                    if response.is_error:
                        details = ' '.join(block.text for block in response.content if getattr(block, 'type', '') == 'text')
                        for name in ('OPENAI_API_KEY', 'TENCENT_DOCS_TOKEN'):
                            secret = os.environ.get(name)
                            if secret:
                                details = details.replace(secret, '[redacted]')
                        raise RuntimeError(f"Live Source MCP {tool}: {details[:300] or 'tool failed'}")
                    data = response.structured_content
                    if not isinstance(data, dict):
                        raise RuntimeError("Live Source MCP returned no structured result")
                    return data.get("result", data)
        try:
            result = asyncio.run(invoke())
        except ExceptionGroup as exc:
            cause = exc
            while isinstance(cause, BaseExceptionGroup):
                cause = cause.exceptions[0]
            raise RuntimeError(f"Live Source MCP: {type(cause).__name__}: {str(cause)[:200]}") from None
        if isinstance(result, dict) and (result.get("fetch_status") == "failed" or result.get("search_status") == "failed"):
            raise RuntimeError(result.get("error", "Article fetch failed"))
        return result
