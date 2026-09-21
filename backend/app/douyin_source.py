"""Auditable Douyin intake and local evidence store.

This is a source adapter and queue, not a browser automation agent. Public page
observations arrive from the allowed local browser or a user. It neither reads
cookies nor invokes private Douyin endpoints. Titles are discovery metadata;
only timestamped evidence can become an extraction document.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .db import utc_now
from .douyin_media import MAX_MEDIA_SECONDS, MAX_UPLOAD_BYTES, MediaError, detect_media, local_capabilities, transcribe_local
from .publication_dates import date_policy, date_status, parse_publication_date


DOUYIN_HOSTS = {"douyin.com", "www.douyin.com", "m.douyin.com", "v.douyin.com", "iesdouyin.com", "www.iesdouyin.com"}
PAUSED_STATUSES = {"waiting_login", "manual_verification", "rate_limited"}
STATUSES = PAUSED_STATUSES | {"awaiting_browser", "unavailable", "media_unavailable", "transcription_failed",
                            "date_unknown", "evidence_insufficient", "ready", "processing", "processed", "outdated", "future", "network_error"}
EVIDENCE_KINDS = {"author_statement", "machine_transcript", "visible_frame", "external_corroboration"}
EVIDENCE_ROLES = {"problem", "workflow", "outcome", "context"}
MAX_STORAGE_BYTES = 2 * 1024 * 1024 * 1024


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _clean(value: Any, maximum: int = 4000) -> str:
    return str(value or "").strip().replace("\x00", "")[:maximum]


def _public_link(value: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("无效的来源链接") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or port not in {None, 80, 443}:
        raise ValueError("仅接受公开 HTTP(S) 来源链接")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or "." not in host or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("不能使用本机或私有地址作为来源")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("不能使用本机或私有地址作为来源")
    return value


def _url_from_text(text: str) -> str:
    matches = re.findall(r"https?://[^\s<>\"'，。；！？、（）]+", _clean(text, 10000))
    for raw in matches:
        value = raw.rstrip(".,;!?)]}）")
        try:
            if urlsplit(value).hostname in DOUYIN_HOSTS:
                return value
        except ValueError:
            continue
    raise ValueError("请粘贴抖音分享文本、短链接或作品链接")


def is_douyin_url(text: str) -> bool:
    try:
        _url_from_text(text)
        return True
    except ValueError:
        return False


def normalize_douyin_link(text: str) -> dict[str, str]:
    """Normalize known public URL forms without following any redirect."""
    value = _public_link(_url_from_text(text))
    parsed = urlsplit(value)
    host = parsed.hostname.lower()
    if parsed.port not in {None, 443} or parsed.scheme != "https":
        raise ValueError("抖音链接必须使用 HTTPS 默认端口")
    if host == "v.douyin.com":
        match = re.fullmatch(r"/([A-Za-z0-9_-]{3,80})/?", parsed.path)
        if not match:
            raise ValueError("无效的抖音短链接")
        return {"aweme_id": "", "canonical_url": f"https://v.douyin.com/{match.group(1)}/", "content_type": "unknown"}
    match = re.fullmatch(r"/(?:share/)?(video|note)/(\d{10,24})/?", parsed.path)
    aweme_id, content_type = (match.group(2), match.group(1)) if match else ("", "video")
    if not aweme_id:
        query = parse_qs(parsed.query)
        candidate = (query.get("modal_id") or query.get("aweme_id") or [""])[0]
        if re.fullmatch(r"\d{10,24}", candidate):
            aweme_id = candidate
    if not aweme_id:
        raise ValueError("链接尚未指向具体作品；作者主页请使用作者发现入口")
    return {"aweme_id": aweme_id, "canonical_url": f"https://www.douyin.com/{content_type}/{aweme_id}", "content_type": content_type}


def normalize_author_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(_public_link(_clean(value, 2000)))
    if parsed.scheme != "https" or parsed.hostname not in {"douyin.com", "www.douyin.com", "m.douyin.com"} or parsed.port not in {None, 443}:
        raise ValueError("作者来源须为抖音公开主页链接")
    match = re.fullmatch(r"/user/([A-Za-z0-9_-]{3,200})/?", parsed.path)
    if not match:
        raise ValueError("无效的抖音作者主页链接")
    return f"https://www.douyin.com/user/{match.group(1)}"


class DouyinStore:
    def __init__(self, db_path: str | Path, media_dir: str | Path | None = None, *, max_storage_bytes: int = MAX_STORAGE_BYTES, readonly: bool = False) -> None:
        self.db_path = str(db_path)
        self.max_storage_bytes = max(1, int(max_storage_bytes))
        self._lock = threading.RLock()
        self.media_dir = (Path(media_dir) if media_dir else Path(self.db_path).parent / "douyin_media").resolve()
        if readonly:
            from urllib.parse import quote
            self.connection = sqlite3.connect("file:" + quote(str(Path(self.db_path).resolve()), safe="/") + "?mode=ro", uri=True, timeout=15, check_same_thread=False)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA busy_timeout=15000")
            return
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path, timeout=15, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=15000")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS douyin_items (
                item_id TEXT PRIMARY KEY, aweme_id TEXT UNIQUE, canonical_url TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '', author_url TEXT NOT NULL DEFAULT '',
                published_at TEXT, published_at_source TEXT NOT NULL DEFAULT '', published_at_evidence TEXT NOT NULL DEFAULT '',
                collected_at TEXT NOT NULL, last_observed_at TEXT, status TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '', queries_json TEXT NOT NULL DEFAULT '[]',
                case_id TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS douyin_aliases (url TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES douyin_items(item_id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS douyin_redirects (old_item_id TEXT PRIMARY KEY, item_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS douyin_observations (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL REFERENCES douyin_items(item_id) ON DELETE CASCADE,
                observed_at TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS douyin_evidence (
                evidence_id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES douyin_items(item_id) ON DELETE CASCADE,
                fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(item_id,fingerprint)
            );
            CREATE TABLE IF NOT EXISTS douyin_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL, stage TEXT NOT NULL,
                status TEXT NOT NULL, reason TEXT NOT NULL, duration_ms INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS douyin_media (
                sha256 TEXT PRIMARY KEY, filename TEXT NOT NULL, mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                imported_at TEXT NOT NULL, last_used_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'available',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS douyin_item_media (
                item_id TEXT NOT NULL REFERENCES douyin_items(item_id) ON DELETE CASCADE,
                sha256 TEXT NOT NULL REFERENCES douyin_media(sha256), original_name TEXT NOT NULL,
                PRIMARY KEY(item_id,sha256)
            );
            CREATE TABLE IF NOT EXISTS douyin_discovery_tasks (
                task_id TEXT PRIMARY KEY, kind TEXT NOT NULL, query TEXT NOT NULL, author_url TEXT NOT NULL,
                limit_count INTEGER NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, progress_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS douyin_settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS douyin_items_status ON douyin_items(status,published_at);
        """)
        self.connection.commit()
        self.recover_stale_processing()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def _resolve_id(self, item_id: str) -> str:
        row = self.connection.execute("SELECT item_id FROM douyin_redirects WHERE old_item_id=?", (item_id,)).fetchone()
        return row[0] if row else item_id

    def _row(self, item_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM douyin_items WHERE item_id=?", (self._resolve_id(item_id),)).fetchone()
        if row is None:
            raise KeyError("抖音作品不存在")
        return row

    def _event(self, item_id: str, stage: str, status: str, reason: str = "", duration_ms: int = 0) -> None:
        self.connection.execute("INSERT INTO douyin_events(item_id,stage,status,reason,duration_ms,created_at) VALUES (?,?,?,?,?,?)",
                                (item_id, _clean(stage, 80), status, _clean(reason), max(0, int(duration_ms)), utc_now()))

    def stage_event(self, item_id: str, stage: str, status: str, reason: str = "", duration_ms: int = 0) -> None:
        with self._lock, self.connection:
            self._event(self._row(item_id)["item_id"], stage, status, reason, duration_ms)

    def import_link(self, text: str, query: str = "", author_url: str = "") -> dict[str, Any]:
        link = normalize_douyin_link(text)
        author_url, query = normalize_author_url(author_url), _clean(query, 300)
        with self._lock, self.connection:
            alias = self.connection.execute("SELECT item_id FROM douyin_aliases WHERE url=?", (link["canonical_url"],)).fetchone()
            item_id = alias[0] if alias else ("douyin:" + link["aweme_id"] if link["aweme_id"] else "douyin:pending:" + hashlib.sha256(link["canonical_url"].encode()).hexdigest()[:20])
            self.connection.execute("""INSERT OR IGNORE INTO douyin_items
                (item_id,aweme_id,canonical_url,author_url,collected_at,status,reason,metadata_json) VALUES (?,?,?,?,?,?,?,?)""",
                                    (item_id, link["aweme_id"] or None, link["canonical_url"], author_url, utc_now(), "awaiting_browser",
                                     "等待本地浏览器读取公开页面", _json({"content_type": link["content_type"]})))
            row = self._row(item_id)
            queries = json.loads(row["queries_json"])
            if query and query not in queries:
                queries.append(query)
            self.connection.execute("UPDATE douyin_items SET queries_json=?,author_url=CASE WHEN author_url='' THEN ? ELSE author_url END WHERE item_id=?",
                                    (_json(queries[:100]), author_url, item_id))
            self.connection.execute("INSERT OR IGNORE INTO douyin_aliases(url,item_id) VALUES (?,?)", (link["canonical_url"], item_id))
            self._event(item_id, "discovery", row["status"], "记录公开作品链接；尚未自动读取视频")
            return self._item(item_id)

    def _item(self, item_id: str, detail: bool = True) -> dict[str, Any]:
        row = dict(self._row(item_id))
        row["id"] = row["item_id"]
        row["source_url"] = row["canonical_url"]
        row["source_type"] = "douyin"
        row["source_name"] = "抖音"
        row["queries"] = json.loads(row.pop("queries_json"))
        row["query"] = row["queries"][-1] if row["queries"] else ""
        row["metadata"] = json.loads(row.pop("metadata_json"))
        row["date_status"] = date_status(row["published_at"])
        row["aliases"] = [value[0] for value in self.connection.execute("SELECT url FROM douyin_aliases WHERE item_id=?", (row["item_id"],))]
        row["evidence"] = [json.loads(value[0]) for value in self.connection.execute("SELECT payload_json FROM douyin_evidence WHERE item_id=? ORDER BY evidence_id", (row["item_id"],))]
        row["evidence"].sort(key=lambda e: (e["start_seconds"], e["end_seconds"], e["kind"]))
        row["media"] = []
        for media in self.connection.execute("""SELECT m.*,im.original_name FROM douyin_media m JOIN douyin_item_media im ON m.sha256=im.sha256
            WHERE im.item_id=? ORDER BY m.imported_at,m.sha256""", (row["item_id"],)):
            value = dict(media)
            value["media_id"] = value["sha256"]
            value["metadata"] = json.loads(value.pop("metadata_json"))
            value.pop("filename")
            row["media"].append(value)
        row["media_available"] = any(m["status"] == "available" for m in row["media"])
        row["duplicate_video_item_ids"] = [value[0] for value in self.connection.execute("""SELECT DISTINCT sibling.item_id
            FROM douyin_item_media own JOIN douyin_item_media sibling ON sibling.sha256=own.sha256
            WHERE own.item_id=? AND sibling.item_id<>?""", (row["item_id"], row["item_id"]))]
        row["local_media_url"] = f"/api/douyin/items/{row['item_id']}/media" if row["media_available"] else None
        if detail:
            row["observations"] = [json.loads(value[0]) for value in self.connection.execute("SELECT payload_json FROM douyin_observations WHERE item_id=? ORDER BY observation_id DESC LIMIT 20", (row["item_id"],))]
            row["events"] = [dict(value) for value in self.connection.execute("SELECT * FROM douyin_events WHERE item_id=? ORDER BY event_id DESC LIMIT 100", (row["item_id"],))]
        return row

    def get_item(self, item_id: str) -> dict[str, Any]:
        with self._lock:
            return self._item(item_id)

    def list_items(self, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute("SELECT item_id FROM douyin_items " + ("WHERE status=? " if status else "") +
                                           "ORDER BY collected_at DESC,item_id LIMIT ?", ((status,) if status else ()) + (max(1, min(int(limit), 500)),)).fetchall()
            return [self._item(row[0], detail=False) for row in rows]

    def pending_items(self, limit: int = 10) -> list[dict[str, Any]]:
        if self.source_state().get("paused"):
            return []
        with self._lock:
            rows = self.connection.execute("""SELECT item_id FROM douyin_items WHERE status IN
                ('awaiting_browser','date_unknown','media_unavailable','evidence_insufficient','network_error')
                ORDER BY COALESCE(published_at,'') DESC,collected_at LIMIT 500""").fetchall()
            items = [self._item(row[0]) for row in rows]
            return [item for item in items if self._retry_due(item["metadata"])][:max(1, min(int(limit), 50))]

    @staticmethod
    def _retry_due(metadata: dict[str, Any]) -> bool:
        return int(metadata.get("network_attempts", 0)) < 3 and str(metadata.get("next_retry_at", "")) <= utc_now()

    def ready_items(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute("SELECT item_id FROM douyin_items WHERE status='ready' ORDER BY published_at DESC,collected_at LIMIT ?", (max(1, min(int(limit), 50)),)).fetchall()
            return [item for row in rows if self._readiness(item := self._item(row[0]))[0] == "ready"]

    def _merge(self, old_id: str, new_id: str, link: dict[str, str]) -> str:
        if old_id == new_id:
            return new_id
        old = self._row(old_id)
        new = self.connection.execute("SELECT * FROM douyin_items WHERE item_id=?", (new_id,)).fetchone()
        if new is None:
            # Parent rows must exist before child foreign keys are moved.
            values = dict(old)
            values.update(item_id=new_id, aweme_id=link["aweme_id"], canonical_url=link["canonical_url"])
            columns = list(values)
            self.connection.execute(f"INSERT INTO douyin_items({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(values.values()))
        else:
            queries = list(dict.fromkeys(json.loads(new["queries_json"]) + json.loads(old["queries_json"])))
            self.connection.execute("UPDATE douyin_items SET queries_json=?,collected_at=MIN(collected_at,?) WHERE item_id=?", (_json(queries), old["collected_at"], new_id))
            for field in ("title", "author", "author_url", "published_at", "published_at_source", "published_at_evidence", "last_observed_at", "case_id"):
                if not new[field] and old[field]:
                    self.connection.execute(f"UPDATE douyin_items SET {field}=? WHERE item_id=?", (old[field], new_id))
            metadata = {**json.loads(old["metadata_json"]), **json.loads(new["metadata_json"])}
            self.connection.execute("UPDATE douyin_items SET metadata_json=? WHERE item_id=?", (_json(metadata), new_id))
        self.connection.execute("UPDATE douyin_aliases SET item_id=? WHERE item_id=?", (new_id, old_id))
        self.connection.execute("UPDATE douyin_observations SET item_id=? WHERE item_id=?", (new_id, old_id))
        self.connection.execute("UPDATE douyin_events SET item_id=? WHERE item_id=?", (new_id, old_id))
        for evidence in self.connection.execute("SELECT fingerprint,payload_json FROM douyin_evidence WHERE item_id=?", (old_id,)).fetchall():
            payload = json.loads(evidence["payload_json"])
            payload["evidence_id"] = hashlib.sha256((new_id + evidence["fingerprint"]).encode()).hexdigest()[:24]
            self.connection.execute("INSERT OR IGNORE INTO douyin_evidence VALUES (?,?,?,?)", (payload["evidence_id"], new_id, evidence["fingerprint"], _json(payload)))
        self.connection.execute("INSERT OR IGNORE INTO douyin_item_media SELECT ?,sha256,original_name FROM douyin_item_media WHERE item_id=?", (new_id, old_id))
        self.connection.execute("UPDATE douyin_redirects SET item_id=? WHERE item_id=?", (new_id, old_id))
        self.connection.execute("INSERT OR REPLACE INTO douyin_redirects VALUES (?,?)", (old_id, new_id))
        self.connection.execute("DELETE FROM douyin_items WHERE item_id=?", (old_id,))
        return new_id

    def _validate_evidence(self, evidence: dict[str, Any], source_url: str, observed_at: str) -> dict[str, Any]:
        if not isinstance(evidence, dict):
            raise ValueError("每条证据必须为结构化对象")
        for boolean_field in ("machine_generated", "verified"):
            if boolean_field in evidence and not isinstance(evidence[boolean_field], bool):
                raise ValueError(f"{boolean_field} 必须是布尔值；字符串不能代表核对完成")
        kind, role = str(evidence.get("kind", "")), str(evidence.get("role", "context"))
        if kind not in EVIDENCE_KINDS or role not in EVIDENCE_ROLES:
            raise ValueError("证据类别或用途无效")
        try:
            start, end = float(evidence["start_seconds"]), float(evidence["end_seconds"])
        except (KeyError, ValueError, TypeError):
            raise ValueError("每条证据必须给出视频起止秒数；图文使用页面顺序并注明画面") from None
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= end <= MAX_MEDIA_SECONDS):
            raise ValueError("证据时间位置无效或超过 30 分钟")
        text = _clean(evidence.get("text"), 8000)
        if not text:
            raise ValueError("证据正文不能为空")
        evidence_source = _clean(evidence.get("source_url") or source_url, 2000)
        _public_link(evidence_source)
        if kind != "external_corroboration":
            evidence_link = normalize_douyin_link(evidence_source)
            item_link = normalize_douyin_link(source_url)
            if item_link["aweme_id"] and evidence_link["aweme_id"] != item_link["aweme_id"]:
                raise ValueError("口述、转写与画面证据必须绑定当前抖音作品")
        elif is_douyin_url(evidence_source):
            external_link = normalize_douyin_link(evidence_source)
            original_link = normalize_douyin_link(source_url)
            if not external_link["aweme_id"]:
                alias = self.connection.execute("SELECT item_id FROM douyin_aliases WHERE url=?", (external_link["canonical_url"],)).fetchone()
                external_id = alias[0] if alias else ""
                if not external_id or ":pending:" in external_id:
                    raise ValueError("外部印证的抖音短链须先由浏览器确认具体作品")
                external_aweme = self._row(external_id)["aweme_id"]
            else:
                external_aweme = external_link["aweme_id"]
            if external_aweme == original_link["aweme_id"]:
                raise ValueError("同一视频的内容不能充当外部印证")
        generated = kind == "machine_transcript" or bool(evidence.get("machine_generated"))
        uncertainty = _clean(evidence.get("uncertainty"), 1000)
        if generated and not uncertainty:
            uncertainty = "机器识别结果，尚未逐项核对"
        result = {"kind": kind, "role": role, "text": text, "start_seconds": start, "end_seconds": end,
                  "source_url": evidence_source, "observed_at": observed_at, "machine_generated": generated,
                  "uncertainty": uncertainty, "verified": bool(evidence.get("verified", False))}
        for key in ("original_text", "language", "engine", "frame_note", "observation_method"):
            if evidence.get(key):
                result[key] = _clean(evidence[key], 8000 if key == "original_text" else 400)
        if evidence.get("page_index") is not None:
            result["page_index"] = max(1, min(int(evidence["page_index"]), 1000))
        return result

    def record_observation(self, item_id: str, observation: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        if not isinstance(observation, dict):
            raise ValueError("观察记录必须为对象")
        with self._lock, self.connection:
            existing = self._row(item_id)
            item_id = existing["item_id"]
            link = normalize_douyin_link(observation.get("canonical_url") or existing["canonical_url"])
            supplied_id = _clean(observation.get("aweme_id"), 30)
            if supplied_id and (not re.fullmatch(r"\d{10,24}", supplied_id) or (link["aweme_id"] and supplied_id != link["aweme_id"])):
                raise ValueError("作品 ID 与规范链接不匹配")
            if supplied_id and not link["aweme_id"]:
                link = normalize_douyin_link(f"https://www.douyin.com/video/{supplied_id}")
            if existing["aweme_id"] and link["aweme_id"] != existing["aweme_id"]:
                raise ValueError("不能把已识别作品的观察记录改绑到另一作品")
            observed_at = _clean(observation.get("observed_at") or utc_now(), 50)
            try:
                stamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                if stamp.tzinfo is None or stamp > datetime.now(timezone.utc) + timedelta(minutes=5):
                    raise ValueError()
            except ValueError:
                raise ValueError("观测时间必须是含时区且不在未来的 ISO 时间") from None
            evidence_values = observation.get("evidence", [])
            if not isinstance(evidence_values, list) or len(evidence_values) > 1000:
                raise ValueError("每次最多录入 1000 条证据")
            validated = [self._validate_evidence(value, link["canonical_url"], observed_at) for value in evidence_values]
            explicit_status = _clean(observation.get("status"), 80)
            if explicit_status and explicit_status not in STATUSES:
                raise ValueError("不支持的抖音处理状态")
            if link["aweme_id"]:
                item_id = self._merge(item_id, "douyin:" + link["aweme_id"], link)
            current = self._row(item_id)
            published, basis, date_evidence = current["published_at"], current["published_at_source"], current["published_at_evidence"]
            if "published_at" in observation:
                parsed = parse_publication_date(observation["published_at"])
                basis = _clean(observation.get("published_at_source"), 200)
                if parsed and (not basis or re.search(r"collect|crawl|import|采集|收集|导入", basis, re.I)):
                    raise ValueError("发布时间必须有页面日期来源，不能用采集或导入时间代替")
                published = parsed.isoformat() if parsed else None
                date_evidence = _clean(observation.get("published_at_evidence") or observation.get("published_at"), 400)
            author_url = normalize_author_url(observation.get("author_url") or current["author_url"])
            title, author = _clean(observation.get("title", current["title"]), 2000), _clean(observation.get("author", current["author"]), 400)
            metadata = json.loads(current["metadata_json"])
            metadata.update({"content_type": _clean(observation.get("content_type") or link["content_type"], 40),
                             "observation_method": _clean(observation.get("observation_method") or "browser_ui", 100)})
            metrics = observation.get("metrics")
            if metrics is not None:
                if not isinstance(metrics, dict):
                    raise ValueError("互动数据必须是对象")
                clean_metrics = {key: _clean(value, 40) for key, value in metrics.items() if key in {"likes", "comments", "shares", "favorites", "views"}}
                metadata["metrics"] = clean_metrics
                metadata["metrics_observed_at"] = observed_at
            if observation.get("duration_seconds") is not None:
                duration = float(observation["duration_seconds"])
                if not math.isfinite(duration) or not 0 <= duration <= MAX_MEDIA_SECONDS:
                    raise ValueError("视频时长无效或超过处理上限")
                metadata["duration_seconds"] = duration
            snapshot = {"canonical_url": link["canonical_url"], "title": title, "author": author, "author_url": author_url,
                        "published_at": published, "published_at_source": basis, "published_at_evidence": date_evidence,
                        "observed_at": observed_at, "observation_method": metadata["observation_method"],
                        "status": explicit_status, "reason": _clean(observation.get("reason")), "evidence_count": len(validated)}
            if metrics is not None:
                snapshot.update(metrics=metadata["metrics"], metrics_observed_at=observed_at)
            # Only explicitly whitelisted fields are persisted. Headers, cookies,
            # arbitrary filesystem paths and a caller's raw HTML are discarded.
            self.connection.execute("""UPDATE douyin_items SET canonical_url=?,title=?,author=?,author_url=?,published_at=?,
                published_at_source=?,published_at_evidence=?,last_observed_at=?,metadata_json=? WHERE item_id=?""",
                                    (link["canonical_url"], title, author, author_url, published, basis, date_evidence, observed_at, _json(metadata), item_id))
            self.connection.execute("INSERT OR IGNORE INTO douyin_aliases(url,item_id) VALUES (?,?)", (link["canonical_url"], item_id))
            self.connection.execute("INSERT INTO douyin_observations(item_id,observed_at,payload_json) VALUES (?,?,?)", (item_id, observed_at, _json(snapshot)))
            added_evidence = 0
            for evidence in validated:
                fingerprint_fields = {key: value for key, value in evidence.items() if key not in {"observed_at", "evidence_id"}}
                fingerprint = hashlib.sha256(_json(fingerprint_fields).encode()).hexdigest()
                evidence["evidence_id"] = hashlib.sha256((item_id + fingerprint).encode()).hexdigest()[:24]
                inserted = self.connection.execute("INSERT OR IGNORE INTO douyin_evidence VALUES (?,?,?,?)", (evidence["evidence_id"], item_id, fingerprint, _json(evidence)))
                added_evidence += inserted.rowcount
            state, reason = self._readiness(self._item(item_id))
            if explicit_status and explicit_status != "ready":
                state, reason = explicit_status, _clean(observation.get("reason")) or reason
            elif current["status"] == "processed" and not added_evidence and current["published_at"] == published:
                state, reason = "processed", current["reason"]
            self._set_status(item_id, state, reason, "browser_observation", round((time.monotonic() - started) * 1000))
            return self._item(item_id)

    @staticmethod
    def _readiness(item: dict[str, Any]) -> tuple[str, str]:
        if not item.get("aweme_id"):
            return "awaiting_browser", "短链接等待本地浏览器确认规范作品链接"
        timing = date_status(item.get("published_at"))
        if timing == "unknown":
            return "date_unknown", "页面未提供可确认的完整发布日期；采集日期不能代替发布时间"
        if timing in {"outdated", "future"}:
            return timing, "作品不在最近一年有效发布日期范围内"
        evidence = item.get("evidence", [])
        if not evidence:
            return ("evidence_insufficient", "素材已导入，等待转写与关键画面核对") if item.get("media_available") else ("media_unavailable", "只取得页面元数据；尚无可用素材或时间段证据")
        # A narrated success claim or a random screenshot is insufficient. This
        # only permits extraction; existing verification/scoring must still run.
        outcome = any(e["kind"] in {"visible_frame", "external_corroboration"} and e["role"] == "outcome" and (not e["machine_generated"] or e["verified"]) for e in evidence)
        workflow = any(e["role"] in {"problem", "workflow"} for e in evidence)
        if len(evidence) < 2 or not outcome or not workflow:
            return "evidence_insufficient", "需要实际问题/工作流证据与结果画面或外部印证；口述与机器转写不能独立证明成功"
        return "ready", "日期与最低证据门槛通过，等待现有抽取、核验、评分及复审"

    def _set_status(self, item_id: str, status: str, reason: str, stage: str, duration_ms: int = 0) -> None:
        row = self._row(item_id)
        metadata = json.loads(row["metadata_json"])
        if status == "network_error":
            attempts = int(metadata.get("network_attempts", 0)) + 1
            metadata.update(network_attempts=attempts, retryable=attempts < 3,
                            next_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=30 * 2 ** min(attempts - 1, 4))).isoformat(timespec="seconds"))
            if attempts >= 3:
                reason = _clean(reason) + "；已达 3 次重试上限，等待人工重新检查"
        elif status != "processing":
            for key in ("network_attempts", "retryable", "next_retry_at"):
                metadata.pop(key, None)
        self.connection.execute("UPDATE douyin_items SET metadata_json=? WHERE item_id=?", (_json(metadata), item_id))
        self.connection.execute("UPDATE douyin_items SET status=?,reason=? WHERE item_id=?", (status, _clean(reason), item_id))
        self._event(item_id, stage, status, reason, duration_ms)
        if status in PAUSED_STATUSES:
            self.connection.execute("INSERT OR REPLACE INTO douyin_settings VALUES ('source_state',?)",
                                    (_json({"paused": True, "status": status, "reason": _clean(reason), "updated_at": utc_now()}),))

    def set_status(self, item_id: str, status: str, reason: str = "", stage: str = "collection", duration_ms: int = 0) -> dict[str, Any]:
        if status not in STATUSES:
            raise ValueError("不支持的抖音处理状态")
        with self._lock, self.connection:
            item = self._item(item_id)
            if status == "ready":
                status, computed = self._readiness(item)
                reason = computed if status != "ready" else reason or computed
            self._set_status(item["item_id"], status, reason, stage, duration_ms)
            return self._item(item["item_id"])

    def mark_processed(self, item_id: str, case_id: str, status: str = "processed") -> dict[str, Any]:
        with self._lock, self.connection:
            item = self._row(item_id)
            item_id = item["item_id"]
            metadata = json.loads(item["metadata_json"])
            metadata["collection_status"] = _clean(status, 80)
            self.connection.execute("UPDATE douyin_items SET case_id=?,metadata_json=? WHERE item_id=?", (_clean(case_id, 200), _json(metadata), item_id))
            self._set_status(item_id, "processed", "已移交工作台现有审核流水线；审核结果以案例记录为准", "collection")
            return self._item(item_id)

    def source_state(self) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute("SELECT value_json FROM douyin_settings WHERE key='source_state'").fetchone()
            return json.loads(row[0]) if row else {"paused": False, "status": "available", "reason": ""}

    def resume_source(self) -> dict[str, Any]:
        """Explicit user/browser action after a login/challenge has been resolved."""
        with self._lock, self.connection:
            self.connection.execute("INSERT OR REPLACE INTO douyin_settings VALUES ('source_state',?)", (_json({"paused": False, "status": "available", "reason": "人工恢复待浏览器重新验证", "updated_at": utc_now()}),))
            self.connection.execute("UPDATE douyin_items SET status='awaiting_browser',reason='人工恢复，等待重新读取' WHERE status IN ('waiting_login','manual_verification','rate_limited')")
            self.connection.execute("UPDATE douyin_discovery_tasks SET status='awaiting_browser',reason='人工恢复，等待重新读取',updated_at=? WHERE status IN ('waiting_login','manual_verification','rate_limited')", (utc_now(),))
            return self.source_state()

    def request_discovery(self, query: str = "", author_url: str = "", limit: int = 5) -> dict[str, Any]:
        query, author_url = _clean(query, 300), normalize_author_url(author_url)
        if not query and not author_url:
            raise ValueError("请输入搜索关键词或作者主页")
        kind = "author" if author_url else "keyword"
        task_id = "dysearch:" + hashlib.sha256((kind + "\n" + query + "\n" + author_url).encode()).hexdigest()[:20]
        with self._lock, self.connection:
            state = self.source_state()
            status = state["status"] if state.get("paused") else "awaiting_browser"
            self.connection.execute("""INSERT OR IGNORE INTO douyin_discovery_tasks
                (task_id,kind,query,author_url,limit_count,status,reason,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                                    (task_id, kind, query, author_url, max(1, min(int(limit), 10)), status, state.get("reason", ""), utc_now(), utc_now()))
            self.connection.execute("UPDATE douyin_discovery_tasks SET status=?,reason=?,limit_count=?,updated_at=? WHERE task_id=? AND status='completed'",
                                    (status, state.get("reason", ""), max(1, min(int(limit), 10)), utc_now(), task_id))
            return self.get_discovery_task(task_id)

    def get_discovery_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute("SELECT * FROM douyin_discovery_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError("抖音发现任务不存在")
            task = dict(row)
            task["progress"] = json.loads(task.pop("progress_json"))
            task["search_url"] = task["author_url"]
            if not task["search_url"]:
                from urllib.parse import quote
                task["search_url"] = "https://www.douyin.com/search/" + quote(task["query"], safe="")
            return task

    def list_discovery_tasks(self, status: str = "", limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute("SELECT task_id FROM douyin_discovery_tasks " + ("WHERE status=? " if status else "") + "ORDER BY updated_at DESC LIMIT ?",
                                           ((status,) if status else ()) + (max(1, min(int(limit), 100)),)).fetchall()
            return [self.get_discovery_task(row[0]) for row in rows]

    def pending_discovery_tasks(self, limit: int = 10) -> list[dict[str, Any]]:
        if self.source_state().get("paused"):
            return []
        return [task for task in self.list_discovery_tasks(limit=100)
                if task["status"] in {"awaiting_browser", "network_error"} and self._retry_due(task["progress"])][:max(1, min(int(limit), 10))]

    def complete_discovery(self, task_id: str, observations: list[dict[str, Any]], status: str = "completed", reason: str = "", duration_ms: int = 0) -> dict[str, Any]:
        """Record a bounded real browser batch, keeping its incremental cursor."""
        with self._lock, self.connection:
            task = self.get_discovery_task(task_id)
            if status not in PAUSED_STATUSES | {"completed", "network_error", "awaiting_browser", "unavailable"}:
                raise ValueError("不支持的发现任务状态")
            if len(observations) > task["limit_count"]:
                raise ValueError("观察数量超过该任务的小批量额度")
            ids = []
            for observation in observations:
                item = self.import_link(observation.get("canonical_url") or observation.get("url") or "", query=task["query"], author_url=task["author_url"])
                item = self.record_observation(item["item_id"], observation)
                ids.append(item["item_id"])
            prior = task["progress"]
            progress = {"item_ids": list(dict.fromkeys(prior.get("item_ids", []) + ids)), "last_batch_count": len(ids),
                        "batches": int(prior.get("batches", 0)) + 1, "duration_ms": max(0, int(duration_ms)), "last_checked_at": utc_now()}
            if status == "network_error":
                attempts = int(prior.get("network_attempts", 0)) + 1
                progress.update(network_attempts=attempts, retryable=attempts < 3,
                                next_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=30 * 2 ** min(attempts - 1, 4))).isoformat(timespec="seconds"))
                if attempts >= 3:
                    reason = _clean(reason) + "；已达 3 次重试上限，等待人工重新检查"
            self.connection.execute("UPDATE douyin_discovery_tasks SET status=?,reason=?,updated_at=?,progress_json=? WHERE task_id=?",
                                    (status, _clean(reason), utc_now(), _json(progress), task_id))
            if status in PAUSED_STATUSES:
                self.connection.execute("INSERT OR REPLACE INTO douyin_settings VALUES ('source_state',?)", (_json({"paused": True, "status": status, "reason": _clean(reason), "updated_at": utc_now()}),))
            return self.get_discovery_task(task_id)

    def import_media(self, item_id: str, data: bytes, filename: str) -> dict[str, Any]:
        extension, mime = detect_media(data, filename)
        digest = hashlib.sha256(data).hexdigest()
        stored_name = digest + extension
        with self._lock, self.connection:
            if not self.connection.in_transaction:
                # Coordinate file writes and quota checks across API/workers,
                # not just threads sharing this Python object.
                self.connection.execute("BEGIN IMMEDIATE")
            item_id = self._row(item_id)["item_id"]
            existing = self.connection.execute("SELECT * FROM douyin_media WHERE sha256=?", (digest,)).fetchone()
            destination = self.media_dir / (existing["filename"] if existing else stored_name)
            if destination.is_symlink() or destination.resolve().parent != self.media_dir:
                raise MediaError("素材存储路径异常")
            occupied = sum(path.stat().st_size for path in self.media_dir.iterdir() if path.is_file() and not path.is_symlink())
            if not destination.exists() and occupied + len(data) > self.max_storage_bytes:
                raise MediaError("素材目录已达到容量上限；请先清理旧素材后重试")
            if not destination.exists():
                # O_EXCL refuses a symlink/racing file. All paths are generated.
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(data)
                except BaseException:
                    destination.unlink(missing_ok=True)
                    raise
            now = utc_now()
            self.connection.execute("""INSERT INTO douyin_media(sha256,filename,mime_type,size_bytes,imported_at,last_used_at,status)
                VALUES (?,?,?,?,?,?,'available') ON CONFLICT(sha256) DO UPDATE SET last_used_at=excluded.last_used_at,status='available'""",
                                    (digest, stored_name, mime, len(data), now, now))
            safe_name = Path(str(filename).replace("\\", "/")).name[:200]
            self.connection.execute("INSERT OR IGNORE INTO douyin_item_media VALUES (?,?,?)", (item_id, digest, safe_name))
            item = self._item(item_id)
            status, reason = self._readiness(item)
            if item["status"] in PAUSED_STATUSES | {"processed"}:
                status, reason = item["status"], item["reason"]
            self._set_status(item_id, status, reason, "media_import")
            return self._item(item_id)

    def media_path(self, item_id: str, media_id: str = "") -> Path:
        with self._lock:
            item_id = self._row(item_id)["item_id"]
            params = (item_id, media_id) if media_id else (item_id,)
            row = self.connection.execute("""SELECT m.* FROM douyin_media m JOIN douyin_item_media im ON m.sha256=im.sha256
                WHERE im.item_id=? AND m.status='available' """ + ("AND m.sha256=? " if media_id else "") +
                                           "ORDER BY CASE WHEN m.mime_type LIKE 'video/%' THEN 0 WHEN m.mime_type LIKE 'audio/%' THEN 1 ELSE 2 END,m.imported_at DESC LIMIT 1", params).fetchone()
            if row is None:
                raise FileNotFoundError("此作品暂无可播放的本地素材")
            path = self.media_dir / row["filename"]
            if path.is_symlink() or path.resolve().parent != self.media_dir or not path.is_file():
                raise FileNotFoundError("本地素材不存在或已被清理")
            return path

    def process_media(self, item_id: str) -> dict[str, Any]:
        started = time.monotonic()
        self.recover_stale_processing()
        with self._lock, self.connection:
            if not self.connection.in_transaction:
                self.connection.execute("BEGIN IMMEDIATE")
            item = self._item(item_id)
            item_id = item["item_id"]
            if item["status"] == "processing":
                return item
            try:
                path = self.media_path(item_id)
            except FileNotFoundError:
                return self.set_status(item_id, "media_unavailable", "没有本地素材；可从正常允许的方式取得后上传", "transcription")
            row = self.connection.execute("SELECT * FROM douyin_media WHERE filename=?", (path.name,)).fetchone()
            metadata = json.loads(row["metadata_json"])
            if metadata.get("transcribed"):
                # Shared media can populate a duplicate video's transcript without
                # repeated decoding, but remains machine evidence for that item.
                segments = metadata.get("segments", [])
                for segment in segments:
                    segment["source_url"] = item["canonical_url"]
                return self.record_observation(item_id, {"evidence": segments, "observation_method": "local_transcription_cache"})
            self.set_status(item_id, "processing", "本地转写处理中", "transcription")
        try:
            segments, info = transcribe_local(path, row["mime_type"])
            with self._lock, self.connection:
                metadata.update(info, transcribed=True, segments=segments)
                self.connection.execute("UPDATE douyin_media SET metadata_json=?,last_used_at=? WHERE sha256=?", (_json(metadata), utc_now(), row["sha256"]))
                result = self.record_observation(item_id, {"evidence": segments, "observation_method": "local_transcription"})
                self._event(item_id, "transcription", "completed", "本地转写已保存；仍需关键画面核对", round((time.monotonic() - started) * 1000))
                return result
        except (MediaError, OSError, ValueError):
            # MediaError messages are already sanitized; arbitrary engine errors
            # are never copied into logs or returned to a model.
            import sys
            error = sys.exc_info()[1]
            reason = str(error) if isinstance(error, MediaError) else "本地转写失败，素材仍保留，可修复后重试"
            return self.set_status(item_id, "transcription_failed", reason, "transcription", round((time.monotonic() - started) * 1000))

    def recover_stale_processing(self, timeout_seconds: int = 600) -> int:
        """A crashed worker must not leave media permanently in processing."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(300, int(timeout_seconds)))).isoformat(timespec="seconds")
        with self._lock, self.connection:
            rows = self.connection.execute("""SELECT i.item_id,MAX(e.created_at) AS started_at
                FROM douyin_items i LEFT JOIN douyin_events e ON e.item_id=i.item_id
                AND e.stage='transcription' AND e.status='processing'
                WHERE i.status='processing' GROUP BY i.item_id""").fetchall()
            stale = [row["item_id"] for row in rows if not row["started_at"] or row["started_at"] < cutoff]
            for item_id in stale:
                self._set_status(item_id, "transcription_failed", "上次本地转写中断或超时，素材和已存证据保留，可重新处理", "recovery")
            return len(stale)

    def cleanup_media(self, max_age_days: int = 30, target_bytes: int | None = None) -> dict[str, Any]:
        """Explicit LRU/age cleanup; structured evidence and observations remain."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, int(max_age_days)))).isoformat(timespec="seconds")
        target = self.max_storage_bytes if target_bytes is None else max(0, int(target_bytes))
        deleted, freed = [], 0
        with self._lock, self.connection:
            if not self.connection.in_transaction:
                self.connection.execute("BEGIN IMMEDIATE")
            rows = self.connection.execute("SELECT * FROM douyin_media WHERE status='available' ORDER BY last_used_at").fetchall()
            total = sum(row["size_bytes"] for row in rows)
            for row in rows:
                if row["last_used_at"] >= cutoff and total <= target:
                    continue
                active = self.connection.execute("SELECT 1 FROM douyin_item_media im JOIN douyin_items i ON i.item_id=im.item_id WHERE im.sha256=? AND i.status='processing'", (row["sha256"],)).fetchone()
                if active:
                    continue
                path = self.media_dir / row["filename"]
                if path.is_symlink() or path.resolve().parent != self.media_dir:
                    continue
                path.unlink(missing_ok=True)
                self.connection.execute("UPDATE douyin_media SET status='evicted' WHERE sha256=?", (row["sha256"],))
                deleted.append(row["sha256"])
                freed += row["size_bytes"]
                total -= row["size_bytes"]
                for item in self.connection.execute("SELECT item_id FROM douyin_item_media WHERE sha256=?", (row["sha256"],)).fetchall():
                    self._event(item[0], "media_cleanup", "evicted", "本地素材已按容量/保留策略清理；原始证据仍保留")
        return {"deleted_count": len(deleted), "freed_bytes": freed, "remaining_bytes": total, "retention_days": max_age_days}

    def storage_summary(self) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute("SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM douyin_media WHERE status='available'").fetchone()
            return {"files": row[0], "used_bytes": row[1], "max_bytes": self.max_storage_bytes,
                    "max_upload_bytes": MAX_UPLOAD_BYTES, "retention_days": 30, "cleanup_policy": "explicit_age_or_capacity_lru",
                    "capabilities": local_capabilities()}

    def evidence_document(self, item_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._item(item_id)
        lines = []
        labels = {"author_statement": "作者口述（未独立验证）", "machine_transcript": "机器转写（需核对）",
                  "visible_frame": "画面可见内容", "external_corroboration": "外部印证"}
        for evidence in item["evidence"]:
            position = f"{evidence['start_seconds']:g}–{evidence['end_seconds']:g}秒"
            page = f"；第{evidence['page_index']}张图" if evidence.get("page_index") else ""
            lines.append(f"[{position}{page} | {labels[evidence['kind']]} | {evidence['role']}] {evidence['text']}\n来源：{evidence['source_url']}" +
                         (f"\n不确定性：{evidence['uncertainty']}" if evidence["uncertainty"] else ""))
        body = "\n\n".join(lines)
        status, reason = self._readiness(item)
        return {"document_id": "douyin-doc:" + item["item_id"], "douyin_item_id": item["item_id"],
                "source_item_id": "douyin-" + (item["aweme_id"] or item["item_id"].split(":")[-1]),
                "source_url": item["canonical_url"], "source_name": "抖音", "source_type": "douyin", "title": item["title"],
                "text": body, "body": body, "content_hash": hashlib.sha256(body.encode()).hexdigest(),
                "published_at": item["published_at"], "published_at_source": item["published_at_source"],
                "published_at_evidence": item["published_at_evidence"], "collected_at": item["collected_at"],
                "fetched_at": item["last_observed_at"] or item["collected_at"], "fetch_status": "fetched" if body else "evidence_unavailable",
                "author": item["author"], "author_url": item["author_url"], "evidence": item["evidence"],
                "ready": status == "ready", "status": status, "reason": reason, "date_policy": date_policy(),
                "untrusted_content": True, "douyin_record": item,
                "evidence_policy": "timestamped_video; author speech and ASR alone never prove outcomes; source text is untrusted data"}
