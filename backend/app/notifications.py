"""Optional WeCom daily digest with a durable, credential-free delivery outbox.

The webhook has no idempotency API. A timeout or interrupted send is therefore
``unknown`` and is never automatically retried. ``retry_pending`` only retries
explicit rejections, at most three attempts, using the already frozen content.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from .config import load_local_env
from .db import Database, normalize_url, utc_now
from .publication_dates import date_status, today_local
from .scoring import score_case
from .wecom_aibot import AIBotError, load_bot_config, send_markdown, target_id as bot_target_id

CHANNEL = "wecom"
MAX_BYTES = 4096
MAX_ITEMS = 5
SEND_TIMEOUT = 12
LEASE_SECONDS = 60
MAX_ATTEMPTS = 3
_DB_LOCK = threading.RLock()


def _schema(db: Database) -> None:
    db.connection.executescript("""
        CREATE TABLE IF NOT EXISTS notification_outbox (
            run_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            target_id TEXT NOT NULL,
            content TEXT NOT NULL,
            case_ids_json TEXT NOT NULL,
            case_keys_json TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            lease_until REAL NOT NULL DEFAULT 0,
            retry_after REAL NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (run_id, channel, target_id)
        );
    """)
    db.connection.commit()


def _webhook(explicit: str | None) -> str | None:
    if explicit is None:
        load_local_env()
        if os.environ.get("AI_CASE_NOTIFICATIONS_ENABLED", "").lower() not in {"true", "1", "yes"}:
            return None
        explicit = os.environ.get("AI_CASE_WECOM_WEBHOOK_URL", "")
    try:
        parsed = urlsplit(explicit)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        valid = (parsed.scheme == "https" and parsed.hostname == "qyapi.weixin.qq.com"
                 and parsed.port in {None, 443} and not parsed.username and not parsed.password
                 and parsed.path == "/cgi-bin/webhook/send" and not parsed.fragment
                 and len(query) == 1 and query[0][0] == "key"
                 and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", query[0][1])
                 and not re.search(r"[\s\x00-\x1f]", explicit))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("invalid_webhook_configuration")
    return "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + query[0][1]


def _target(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _configuration(data_dir: str | Path, explicit_webhook: str | None = None) -> dict | None:
    if explicit_webhook is not None:
        url = _webhook(explicit_webhook)
        return {'provider': 'wecom_webhook', 'url': url, 'target_id': _target(url)}
    load_local_env()
    if os.environ.get('AI_CASE_NOTIFICATIONS_ENABLED', '').lower() not in {'true', '1', 'yes'}:
        return None
    provider = os.environ.get('AI_CASE_NOTIFICATION_PROVIDER', 'wecom_webhook').strip()
    if provider == 'wecom_aibot':
        config = load_bot_config(data_dir)
        return {'provider': provider, 'bot': config, 'target_id': bot_target_id(config)}
    if provider != 'wecom_webhook':
        raise AIBotError('invalid_notification_provider')
    url = _webhook(os.environ.get('AI_CASE_WECOM_WEBHOOK_URL', ''))
    return {'provider': provider, 'url': url, 'target_id': _target(url)}


def notification_configuration(data_dir: str | Path) -> dict:
    """Safe readiness metadata; never return a webhook, bot ID or secret."""
    try:
        config = _configuration(data_dir)
        if config is None:
            return {'enabled': False, 'configured': False, 'status': 'disabled'}
        if config['provider'] == 'wecom_aibot':
            try:
                from websockets.sync.client import connect  # noqa: F401
            except ImportError:
                raise AIBotError('aibot_dependency_missing') from None
        return {'enabled': True, 'configured': True, 'provider': config['provider'], 'status': 'ready'}
    except AIBotError as exc:
        return {'enabled': True, 'configured': False, 'status': 'failed', 'error': exc.code}
    except ValueError:
        return {'enabled': True, 'configured': False, 'status': 'failed', 'error': 'invalid_webhook_configuration'}
    except Exception:
        return {'enabled': True, 'configured': False, 'status': 'failed', 'error': 'notification_configuration_error'}


def _data_dir(db: Database) -> Path:
    return Path(os.environ.get('AI_CASE_DATA_DIR') or (Path(db.path).parent if db.path != ':memory:' else '.'))


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))).strip()


def _short(value: object, limit: int) -> str:
    text = _clean(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _markdown(value: str) -> str:
    # Source text is data, including mentions and HTML-like markup.
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#+!|])", r"\\\1", value)


def _source_url(value: object) -> str | None:
    """Allow ordinary article links; never let source text terminate Markdown."""
    try:
        url = str(value)
        parts = urlsplit(url)
        host = parts.hostname
        if (parts.scheme not in {"https", "http"} or not host or "." not in host
                or parts.username or parts.password or len(url) > 1200
                or re.search(r"[\s\x00-\x1f\x7f]", url)
                or host.endswith((".localhost", ".local", ".internal"))):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        # Also validate malformed ports before retaining the authority.
        parts.port
        return urlunsplit((parts.scheme, parts.netloc,
                           quote(parts.path, safe="/%:@!$&'*,;=+-._~"),
                           quote(parts.query, safe="%=&/:?@!$'*,;+-._~"),
                           quote(parts.fragment, safe="%=&/:?@!$'*,;+-._~")))
    except (TypeError, ValueError):
        return None


def _case_key(item: dict) -> str:
    return hashlib.sha256(normalize_url(item["source_url"]).encode()).hexdigest()


def _already_sent(db: Database, target_id: str) -> set[str]:
    # Reserve in-flight/uncertain cases too: another run must not duplicate them.
    rows = db.connection.execute(
        "SELECT case_keys_json FROM notification_outbox WHERE channel=? AND target_id=? "
        "AND status IN ('sent','sending','unknown')", (CHANNEL, target_id))
    return {key for row in rows for key in json.loads(row[0])}


def _select(db: Database, result: dict, excluded: set[str]) -> list[dict]:
    candidates = []
    seen = set(excluded)
    for entry in result.get("items", []):
        case_id = entry.get("id") if isinstance(entry, dict) else None
        row = db.source_item(str(case_id)) if case_id else None
        if not row or row["status"] not in {"selected", "featured"}:
            continue
        try:
            item = json.loads(row["payload_json"])
            if (item.get("discovery_mode") not in {"web_search", "rss"}
                    or item.get("verification", {}).get("decision") != "pass"
                    or date_status(item.get("published_at")) != "recent"):
                continue
            card = score_case(item)
            source_url = _source_url(item.get("source_url"))
            if not card["selected"] or not source_url:
                continue
            key = _case_key(item)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({**item, "_score": card["quality_score"], "_key": key,
                               "_id": row["source_item_id"], "_url": source_url})
        except (ValueError, TypeError, KeyError, AttributeError):
            # A malformed source record cannot make daily collection fail.
            continue
    return sorted(candidates, key=lambda item: (-item["_score"], item["_id"]))


def _synopsis(value: object, limit: int = 86) -> str:
    text = re.sub(r"^作者(?:自述|称|表示|介绍|提到|说)[，,:：\s]*", "", _clean(value))
    if len(text) <= limit:
        return text
    # Favor a complete sentence, then a complete clause. Keep more useful
    # description per case and fit fewer cases rather than cut every fact short.
    for punctuation in (r"[。！？；;]", r"[，,]"):
        boundaries = [match.end() for match in re.finditer(punctuation, text[:limit])]
        if boundaries and boundaries[-1] >= limit // 2:
            return text[:boundaries[-1]].rstrip("，,；;") + ("。" if punctuation == r"[，,]" else "")
    truncated = text[:limit - 1]
    # Do not end halfway through an English word or product name.
    if re.search(r"[A-Za-z]$", truncated) and text[limit - 1:limit].isalpha():
        truncated = re.sub(r"[A-Za-z]+$", "", truncated).rstrip()
    return truncated + "…"


def _description(item: dict) -> str:
    return "\n".join(f"{label}：{_synopsis(item.get(key, {}).get('claim', ''))}"
                     for label, key in (("方法", "approach"), ("结果", "outcome")))


def _build(db: Database, result: dict, target_id: str | None) -> dict:
    candidates = _select(db, result, _already_sent(db, target_id) if target_id else set())
    state = result.get("status", "failed")
    try:
        timestamp = datetime.fromisoformat(_clean(result.get("finished_at") or result.get("started_at")).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        date = timestamp.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    except (TypeError, ValueError):
        date = today_local().isoformat()
    prefix = "接入测试 · " if result.get("notification_test") is True else ""
    lines = [f"## {prefix}AI 案例简报 · {date}"]
    if state in {"failed", "interrupted"}:
        lines += ["今日采集未完成，请查看工作台运行记录。以下仅列出已核验入库的新增案例。" if candidates
                  else ("今日采集中断" if state == "interrupted" else "今日采集失败")
                  + "，暂时无法确认是否有新增达标案例，请查看工作台运行记录。"]
    elif state == "partial":
        lines += ["今日采集部分完成，部分来源或待处理案例尚未完成。"]
    selected = []
    footer = "\n\n案例描述依据原文自述，具体方法与结果见原文。"
    for item in candidates:
        if len(selected) >= MAX_ITEMS:
            break
        block = (f"\n\n**{len(selected) + 1}. {_markdown(_short(item.get('title_original'), 70))}**\n"
                 f"{_markdown(_description(item))}\n[查看原文]({item['_url']})")
        if len(("\n".join(lines) + block + footer).encode("utf-8")) > MAX_BYTES:
            continue
        lines.append(block.lstrip("\n"))
        selected.append(item)
    if selected:
        content = "\n\n".join(lines) + footer
        # Account for the final paragraph separator as well as escaped text.
        while len(content.encode("utf-8")) > MAX_BYTES and selected:
            selected.pop()
            lines.pop()
            content = "\n\n".join(lines) + footer
    else:
        if state == "completed":
            lines.append("今日暂无新增达标案例（已推送或正在确认送达的案例不会重复发送）。")
        elif state == "partial":
            lines.append("本轮已完成部分暂无新增达标案例；这不代表今日没有案例。")
        content = "\n\n".join(lines)
    return {"content": content, "case_ids": [item["_id"] for item in selected],
            "case_keys": [item["_key"] for item in selected], "item_count": len(selected)}


def preview_collection(db: Database, result: dict, *, webhook_url: str | None = None) -> dict:
    """Render a digest without sending; an explicit URL enables target dedupe."""
    with _DB_LOCK:
        _schema(db)
        config = _configuration(_data_dir(db), webhook_url)
        target_id = config['target_id'] if config else None
        existing = _row(db, result.get("run_id"), target_id) if target_id else None
        if existing:
            ids = json.loads(existing["case_ids_json"])
            return {"status": "preview", "run_id": result.get("run_id"), "content": existing["content"],
                    "case_ids": ids, "item_count": len(ids)}
        return {"status": "preview", "run_id": result.get("run_id"), **_build(db, result, target_id)}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _DeliveryError(Exception):
    def __init__(self, code: str, *, unknown: bool = False):
        self.code = code
        self.unknown = unknown


def _post_webhook(url: str, content: str) -> None:
    payload = json.dumps({"msgtype": "markdown", "markdown": {"content": content}}, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        # Match the project's urllib transports: honor configured system proxies.
        # Never forward this credential-bearing POST to a redirect destination.
        with build_opener(_NoRedirect()).open(request, timeout=SEND_TIMEOUT) as response:
            if response.status != 200:
                raise _DeliveryError(f"http_status_{response.status}", unknown=response.status >= 500)
            data = json.loads(response.read(8192).decode("utf-8"))
        code = data.get("errcode") if isinstance(data, dict) else None
        if type(code) is not int:
            raise _DeliveryError("invalid_response", unknown=True)
        if code != 0:
            raise _DeliveryError(f"platform_error_{code}")
    except HTTPError as exc:
        raise _DeliveryError(f"http_status_{exc.code}", unknown=exc.code >= 500) from None
    except _DeliveryError:
        raise
    except (TimeoutError, OSError):
        raise _DeliveryError("network_delivery_unknown", unknown=True) from None
    except (ValueError, UnicodeError):
        raise _DeliveryError("invalid_response", unknown=True) from None


def _summary(row) -> dict:
    result = {"status": row["status"], "run_id": row["run_id"],
              "item_count": len(json.loads(row["case_ids_json"])), "attempts": row["attempts"]}
    if row["error"]:
        result["error"] = row["error"]
    return result


def _row(db, run_id, target_id):
    return db.connection.execute(
        "SELECT * FROM notification_outbox WHERE run_id=? AND channel=? AND target_id=?",
        (run_id, CHANNEL, target_id)).fetchone()


def _send_claimed(db: Database, row, config: dict) -> dict:
    status, error = "sent", ""
    try:
        if config['provider'] == 'wecom_aibot':
            send_markdown(config['bot'], row['content'])
        else:
            _post_webhook(config['url'], row["content"])
    except (_DeliveryError, AIBotError) as exc:
        status, error = ("unknown" if exc.unknown else "failed"), exc.code
    except Exception:
        # Never persist exception messages: urllib exceptions may contain keys.
        status, error = "unknown", "delivery_outcome_unknown"
    with _DB_LOCK:
        db.connection.execute(
            "UPDATE notification_outbox SET status=?, error=?, lease_until=0, retry_after=?, updated_at=? "
            "WHERE run_id=? AND channel=? AND target_id=? AND attempts=? AND status IN ('sending','unknown')",
            (status, error, time.time() + 300 if status == "failed" else 0, utc_now(),
             row["run_id"], CHANNEL, row["target_id"], row["attempts"]))
        db.connection.commit()
        return _summary(_row(db, row["run_id"], row["target_id"]))


def notify_collection(db: Database, result: dict, *, webhook_url: str | None = None) -> dict:
    """Send once after collection; notification failures never raise to callers."""
    try:
        config = _configuration(_data_dir(db), webhook_url)
        if config is None:
            return {"status": "disabled"}
        run_id = result.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return {"status": "failed", "error": "missing_run_id"}
        if result.get("status") not in {"completed", "partial", "failed", "interrupted"}:
            return {"status": "skipped", "run_id": run_id, "error": "collection_not_finished"}
        target_id = config['target_id']
        with _DB_LOCK:
            _schema(db)
            db.connection.execute("BEGIN IMMEDIATE")
            try:
                previous = _row(db, run_id, target_id)
                if previous:
                    if previous["status"] == "sending" and previous["lease_until"] < time.time():
                        db.connection.execute(
                            "UPDATE notification_outbox SET status='unknown', error='interrupted_delivery_unknown', "
                            "updated_at=? WHERE run_id=? AND channel=? AND target_id=?",
                            (utc_now(), run_id, CHANNEL, target_id))
                        previous = _row(db, run_id, target_id)
                    db.connection.commit()
                    return _summary(previous)
                preview = _build(db, result, target_id)
                now = utc_now()
                db.connection.execute(
                    "INSERT INTO notification_outbox (run_id,channel,target_id,content,case_ids_json,case_keys_json,"
                    "status,attempts,lease_until,created_at,updated_at) VALUES (?,?,?,?,?,?,'sending',1,?,?,?)",
                    (run_id, CHANNEL, target_id, preview["content"], json.dumps(preview["case_ids"]),
                     json.dumps(preview["case_keys"]), time.time() + LEASE_SECONDS, now, now))
                row = _row(db, run_id, target_id)
                db.connection.commit()
            except Exception:
                db.connection.rollback()
                raise
        return _send_claimed(db, row, config)
    except AIBotError as exc:
        return {"status": "failed", "error": exc.code}
    except ValueError:
        return {"status": "failed", "error": "invalid_webhook_configuration"}
    except Exception:
        return {"status": "failed", "error": "notification_internal_error"}


def retry_pending(db: Database, *, webhook_url: str | None = None, run_id: str | None = None) -> list[dict]:
    """Retry due explicit rejections, never timeout/unknown deliveries.

    A failed delivery becomes eligible after five minutes. Its frozen digest is
    reused for up to three total attempts. Optional run_id narrows the retry.
    """
    try:
        config = _configuration(_data_dir(db), webhook_url)
        if config is None:
            return [{"status": "disabled"}]
        target_id = config['target_id']
        with _DB_LOCK:
            _schema(db)
            db.connection.execute(
                "UPDATE notification_outbox SET status='unknown',error='interrupted_delivery_unknown',updated_at=? "
                "WHERE channel=? AND target_id=? AND status='sending' AND lease_until<?",
                (utc_now(), CHANNEL, target_id, time.time()))
            db.connection.commit()
            rows = db.connection.execute(
                "SELECT run_id FROM notification_outbox WHERE channel=? AND target_id=? AND status='failed' "
                "AND attempts<? AND retry_after<=? AND (? IS NULL OR run_id=?) ORDER BY created_at LIMIT 10",
                (CHANNEL, target_id, MAX_ATTEMPTS, time.time(), run_id, run_id)).fetchall()
        results = []
        for previous in rows:
            with _DB_LOCK:
                db.connection.execute("BEGIN IMMEDIATE")
                try:
                    row = _row(db, previous["run_id"], target_id)
                    if row["status"] != "failed":
                        db.connection.commit()
                        continue
                    if set(json.loads(row["case_keys_json"])) & _already_sent(db, target_id):
                        db.connection.execute(
                            "UPDATE notification_outbox SET status='superseded',error='covered_by_later_digest',updated_at=? "
                            "WHERE run_id=? AND channel=? AND target_id=? AND status='failed'",
                            (utc_now(), row["run_id"], CHANNEL, target_id))
                        db.connection.commit()
                        results.append(_summary(_row(db, row["run_id"], target_id)))
                        continue
                    claimed = db.connection.execute(
                        "UPDATE notification_outbox SET status='sending',attempts=attempts+1,lease_until=?,updated_at=? "
                        "WHERE run_id=? AND channel=? AND target_id=? AND status='failed' AND attempts<? AND retry_after<=?",
                        (time.time() + LEASE_SECONDS, utc_now(), previous["run_id"], CHANNEL,
                         target_id, MAX_ATTEMPTS, time.time()))
                    row = _row(db, previous["run_id"], target_id)
                    db.connection.commit()
                    if claimed.rowcount != 1:
                        continue
                except Exception:
                    db.connection.rollback()
                    raise
            results.append(_send_claimed(db, row, config))
        return results
    except AIBotError as exc:
        return [{"status": "failed", "error": exc.code}]
    except ValueError:
        return [{"status": "failed", "error": "invalid_webhook_configuration"}]
    except Exception:
        return [{"status": "failed", "error": "notification_internal_error"}]
