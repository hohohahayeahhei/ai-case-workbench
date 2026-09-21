"""Public-web MCP tools used by the live collection path."""
import sys
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote
from mcp.server.mcpserver import MCPServer

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.agent_model import OpenAICompatibleModel
from backend.app.discovery_agent import SourceDiscoveryAgent
from backend.app.scoring import score_case as compute_score
from backend.app.agent_skills import skill_text
from backend.app.config import load_local_env
from backend.app.douyin_source import DouyinStore, is_douyin_url, normalize_author_url

mcp = MCPServer("AI Case Live Sources", version="1.0.0")


@mcp.tool()
def search_web(query: str, max_results: int = 8) -> list[dict[str, Any]] | dict[str, Any]:
    """Search the public web using the configured model's actual web_search tool."""
    if not query.strip() or not 1 <= max_results <= 20:
        raise ValueError("Provide a query and max_results between 1 and 20")
    try:
        return OpenAICompatibleModel().search_web(query, max_results)
    except (OSError, ValueError, RuntimeError) as exc:
        return {"search_status": "failed", "error": OpenAICompatibleModel._redact(f"{type(exc).__name__}: {exc}")[:700]}


@mcp.tool()
def fetch_page(url: str) -> dict[str, Any]:
    """Fetch bounded public HTML/text as untrusted evidence, with a content hash."""
    if is_douyin_url(url):
        return {'url': url, 'fetch_status': 'failed', 'source_status': 'awaiting_browser',
                'error': '抖音需要本地浏览器观察或允许取得的本地素材；fetch_page 不将标题、简介当作视频正文。'}
    try:
        return SourceDiscoveryAgent(ROOT).fetch_page(url)
    except (OSError, ValueError, RuntimeError) as exc:
        return {"url": url, "fetch_status": "failed", "error": OpenAICompatibleModel._redact(f"{type(exc).__name__}: {exc}")[:700]}


@mcp.tool()
def score_case(case: dict[str, Any]) -> dict[str, Any]:
    """Apply versioned deterministic rules to an evidence-bearing case."""
    return compute_score(case)


def _read_douyin(method: str, **arguments):
    """Open only the configured local catalog in SQLite read-only mode."""
    if method not in {'list_items', 'get_item', 'list_discovery_tasks', 'source_state'}:
        raise ValueError('Unsupported local video read')
    load_local_env()
    path = Path(os.environ.get('AI_CASE_DATA_DIR', str(ROOT / 'data/runtime'))) / 'catalog.db'
    if not path.is_file():
        return None if method in {'get_item', 'source_state'} else []
    try:
        with closing(DouyinStore(path, readonly=True)) as store:
            return getattr(store, method)(**arguments)
    except KeyError:
        return None
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        return None if method in {'get_item', 'source_state'} else []


@mcp.tool()
def douyin_search(query: str = '', author_url: str = '', max_results: int = 5) -> dict[str, Any]:
    """Read known local hits and a browser search plan. Does not run a browser or write tasks."""
    query = query.strip()
    if not (query or author_url) or len(query) > 200 or not 1 <= max_results <= 10:
        raise ValueError('Provide a query or public author URL and max_results between 1 and 10')
    if author_url:
        author_url = normalize_author_url(author_url)
        target = author_url
    else:
        target = 'https://www.douyin.com/search/' + quote(query, safe='')
    hits = [record for record in _read_douyin('list_items', limit=100)
            if (not query or query.lower() in str(record.get('title', '')).lower()
                or query in record.get('queries', []) or query == record.get('query'))
            and (not author_url or author_url == record.get('author_url'))]
    return {'search_status': 'browser_required', 'executed': False, 'read_only': True,
            'browser_url': target, 'max_results': max_results, 'items': hits[:max_results],
            'instructions': '由本地允许的浏览器会话读取公开页面；登录/验证码/限流时暂停并记录。工具本身不执行浏览器、不获取Cookie、不写入数据库。'}


@mcp.tool()
def douyin_read(item_id: str) -> dict[str, Any]:
    """Read one already stored video observation with timed evidence; no remote fetch."""
    if not item_id.startswith('douyin:') or len(item_id) > 100:
        raise ValueError('Provide a local Douyin item_id')
    record = _read_douyin('get_item', item_id=item_id)
    return {'read_only': True, 'found': record is not None, 'item': record}


@mcp.tool()
def douyin_status(limit: int = 20) -> dict[str, Any]:
    """Read local video/task processing state, without advancing it."""
    if not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    items = _read_douyin('list_items', limit=limit)
    tasks = _read_douyin('list_discovery_tasks', limit=limit)
    return {'read_only': True, 'browser_automation': False, 'items': items, 'tasks': tasks,
            'source_state': _read_douyin('source_state')}


@mcp.resource("policy://discovery")
def discovery_policy() -> str:
    return skill_text("scout")


@mcp.resource('policy://douyin')
def douyin_policy() -> str:
    return skill_text('douyin')


@mcp.prompt()
def verification_prompt() -> str:
    return skill_text("verify")


if __name__ == "__main__":
    mcp.run(transport="stdio")
