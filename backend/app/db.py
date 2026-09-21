"""Small SQLite persistence layer for the local portfolio demo."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Union


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Union[str, Path] = ":memory:") -> None:
        self.path = str(path)
        # FastAPI may execute sync endpoints in a worker thread. The database
        # object is still owned by one workbench instance; the lock is provided
        # by SQLite's serialized writes for this local demo.
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                status TEXT NOT NULL,
                scenario TEXT NOT NULL,
                created_at TEXT NOT NULL,
                connector_mode TEXT NOT NULL DEFAULT 'mock',
                llm_mode TEXT NOT NULL DEFAULT 'mock',
                dataset_mode TEXT NOT NULL DEFAULT 'fixture',
                knowledge_mode TEXT NOT NULL DEFAULT 'local_sqlite'
            );
            CREATE TABLE IF NOT EXISTS run_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                actor TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                title_original TEXT NOT NULL,
                source_url TEXT NOT NULL,
                normalized_url TEXT NOT NULL,
                status TEXT NOT NULL,
                verification_reason TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL,
                quality_score INTEGER NOT NULL DEFAULT 0,
                score_tier TEXT NOT NULL DEFAULT 'candidate',
                selected INTEGER NOT NULL DEFAULT 0,
                score_version TEXT NOT NULL DEFAULT '',
                scorecard_json TEXT NOT NULL DEFAULT '{}',
                cluster_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_items (
                source_item_id TEXT PRIMARY KEY,
                source_url TEXT NOT NULL,
                normalized_url TEXT NOT NULL,
                source_name TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                discovered_at TEXT NOT NULL,
                etag TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'candidate',
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collection_runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS raw_documents (
                document_id TEXT PRIMARY KEY,
                source_item_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                fetch_status TEXT NOT NULL DEFAULT 'fetched'
            );
            CREATE TABLE IF NOT EXISTS case_clusters (
                cluster_id TEXT PRIMARY KEY,
                canonical_case_id TEXT NOT NULL,
                title TEXT NOT NULL,
                item_ids_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_states (
                sync_name TEXT PRIMARY KEY,
                snapshot_cursor TEXT NOT NULL DEFAULT '',
                page_cursor TEXT NOT NULL DEFAULT '',
                etag TEXT NOT NULL DEFAULT '',
                complete INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS content_versions (
                content_version_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                status TEXT NOT NULL,
                body TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivery_tasks (
                delivery_task_id TEXT PRIMARY KEY,
                content_version_id TEXT NOT NULL REFERENCES content_versions(content_version_id),
                channel TEXT NOT NULL,
                target TEXT NOT NULL,
                status TEXT NOT NULL,
                external_id TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                UNIQUE(content_version_id, channel, target)
            );
            CREATE TABLE IF NOT EXISTS delivery_receipts (
                receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_task_id TEXT NOT NULL REFERENCES delivery_tasks(delivery_task_id),
                status TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(runs)").fetchall()}
        if "connector_mode" not in columns:
            self.connection.execute("ALTER TABLE runs ADD COLUMN connector_mode TEXT NOT NULL DEFAULT 'mock'")
        if "llm_mode" not in columns:
            self.connection.execute("ALTER TABLE runs ADD COLUMN llm_mode TEXT NOT NULL DEFAULT 'mock'")
        if "dataset_mode" not in columns:
            self.connection.execute("ALTER TABLE runs ADD COLUMN dataset_mode TEXT NOT NULL DEFAULT 'fixture'")
        if "knowledge_mode" not in columns:
            self.connection.execute("ALTER TABLE runs ADD COLUMN knowledge_mode TEXT NOT NULL DEFAULT 'local_sqlite'")
        document_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(raw_documents)").fetchall()}
        if "metadata_json" not in document_columns:
            self.connection.execute("ALTER TABLE raw_documents ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
        case_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(cases)").fetchall()}
        migrations = {
            "quality_score": "INTEGER NOT NULL DEFAULT 0",
            "score_tier": "TEXT NOT NULL DEFAULT 'candidate'",
            "selected": "INTEGER NOT NULL DEFAULT 0",
            "score_version": "TEXT NOT NULL DEFAULT ''",
            "scorecard_json": "TEXT NOT NULL DEFAULT '{}'",
            "cluster_id": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in migrations.items():
            if name not in case_columns:
                self.connection.execute(f"ALTER TABLE cases ADD COLUMN {name} {definition}")
        self.connection.commit()

    def create_run(self, run_id: str, topic: str, scenario: str, connector_mode: str = "mock", llm_mode: str = "mock", dataset_mode: str = "fixture", knowledge_mode: str = "local_sqlite") -> None:
        self.connection.execute(
            "INSERT INTO runs (run_id, topic, status, scenario, created_at, connector_mode, llm_mode, dataset_mode, knowledge_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, topic, "running", scenario, utc_now(), connector_mode, llm_mode, dataset_mode, knowledge_mode),
        )
        self.connection.commit()

    def update_run(self, run_id: str, status: str) -> None:
        self.connection.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id))
        self.connection.commit()

    def event(
        self,
        run_id: str,
        actor: str,
        event_type: str,
        status: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        event_metadata = dict(metadata or {})
        event_metadata.setdefault("trace_id", run_id)
        event_metadata.setdefault("span_id", f"{actor}:{event_type}")
        event_metadata.setdefault("attempt", 1)
        self.connection.execute(
            """INSERT INTO run_events
            (run_id, actor, event_type, status, message, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, actor, event_type, status, message, json.dumps(event_metadata, ensure_ascii=False), utc_now()),
        )
        self.connection.commit()

    def save_case(self, run_id: str, case: Dict[str, Any], status: str, reason: str, scorecard: Optional[Dict[str, Any]] = None, cluster_id: str = "") -> None:
        score = scorecard or {}
        self.connection.execute(
            """INSERT OR REPLACE INTO cases
            (case_id, run_id, title_original, source_url, normalized_url, status,
             verification_reason, evidence_json, quality_score, score_tier, selected,
             score_version, scorecard_json, cluster_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                case["case_id"], run_id, case["title_original"], case["source_url"],
                normalize_url(case["source_url"]), status, reason,
                json.dumps(case, ensure_ascii=False), int(score.get("quality_score") or 0),
                str(score.get("tier", "candidate")), int(bool(score.get("selected", False))),
                str(score.get("score_version", "")), json.dumps(score, ensure_ascii=False),
                cluster_id, utc_now(),
            ),
        )
        self.connection.commit()

    def save_source_item(self, item: Dict[str, Any], status: str = "candidate", etag: str = "") -> None:
        source_url = str(item.get("source_url", ""))
        self.connection.execute(
            """INSERT OR REPLACE INTO source_items
            (source_item_id, source_url, normalized_url, source_name, source_type, title,
             discovered_at, etag, status, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["case_id"], source_url, normalize_url(source_url),
                str(item.get("source_name", "")), str(item.get("source_type", "")),
                str(item.get("title_original", "")), utc_now(), etag, status,
                json.dumps(item, ensure_ascii=False),
            ),
        )
        self.connection.commit()

    def save_raw_document(self, document: Dict[str, Any]) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO raw_documents
            (document_id, source_item_id, source_url, content_hash, title, body, fetched_at, fetch_status, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                document["document_id"], document["source_item_id"], document["source_url"],
                document["content_hash"], document.get("title", ""), document.get("text", ""),
                utc_now(), document.get("fetch_status", "fetched"),
                json.dumps({key: document[key] for key in ("published_at", "published_at_source", "published_at_evidence", "updated_at", "updated_at_source", "updated_at_evidence", "fetch_strategy", "fetch_strategy_label", "fetched_url") if key in document}, ensure_ascii=False),
            ),
        )
        self.connection.commit()

    def raw_documents(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM raw_documents ORDER BY fetched_at DESC LIMIT ?", (limit,)).fetchall()

    def save_cluster(self, cluster_id: str, canonical_case_id: str, title: str, item_ids: list[str]) -> None:
        previous = self.connection.execute("SELECT * FROM case_clusters WHERE cluster_id = ?", (cluster_id,)).fetchone()
        if previous:
            item_ids = list(dict.fromkeys(json.loads(previous["item_ids_json"]) + item_ids))
            canonical_case_id = previous["canonical_case_id"]
        self.connection.execute(
            """INSERT OR REPLACE INTO case_clusters
            (cluster_id, canonical_case_id, title, item_ids_json, updated_at)
            VALUES (?, ?, ?, ?, ?)""",
            (cluster_id, canonical_case_id, title, json.dumps(item_ids, ensure_ascii=False), utc_now()),
        )
        self.connection.commit()

    def scorecards(self, run_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT case_id, quality_score, score_tier, selected, score_version, scorecard_json, cluster_id FROM cases WHERE run_id = ? ORDER BY quality_score DESC, case_id",
            (run_id,),
        ).fetchall()

    def source_items(self, statuses: Optional[list[str]] = None, limit: int = 100) -> list[sqlite3.Row]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            return self.connection.execute(
                f"SELECT * FROM source_items WHERE status IN ({placeholders}) ORDER BY discovered_at DESC LIMIT ?",
                (*statuses, limit),
            ).fetchall()
        return self.connection.execute(
            "SELECT * FROM source_items ORDER BY discovered_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def source_item(self, source_item_id: str) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM source_items WHERE source_item_id = ?", (source_item_id,)
        ).fetchone()

    def source_by_url(self, url: str) -> Optional[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM source_items WHERE normalized_url = ? ORDER BY (json_extract(payload_json, '$.discovery_mode') IN ('web_search','rss')) DESC, discovered_at DESC", (normalize_url(url),)).fetchone()

    def save_collection_run(self, result: Dict[str, Any]) -> None:
        self.connection.execute("INSERT OR REPLACE INTO collection_runs VALUES (?, ?, ?, ?)",
                                (result["run_id"], result["status"], json.dumps(result, ensure_ascii=False), utc_now()))
        self.connection.commit()

    def collection_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute("SELECT payload_json FROM collection_runs WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def collection_history(self, limit: int = 20) -> list[Dict[str, Any]]:
        return [json.loads(row[0]) for row in self.connection.execute("SELECT payload_json FROM collection_runs ORDER BY updated_at DESC LIMIT ?", (limit,))]

    def update_source_item(self, source_item_id: str, status: str, payload: Optional[Dict[str, Any]] = None) -> None:
        if payload is None:
            self.connection.execute("UPDATE source_items SET status = ? WHERE source_item_id = ?", (status, source_item_id))
        else:
            self.connection.execute(
                "UPDATE source_items SET status = ?, payload_json = ?, title = ? WHERE source_item_id = ?",
                (status, json.dumps(payload, ensure_ascii=False), str(payload.get("title_original", payload.get("title", ""))), source_item_id),
            )
        self.connection.commit()

    def clusters(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM case_clusters ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def save_sync_state(self, sync_name: str, snapshot_cursor: str = "", page_cursor: str = "", etag: str = "", complete: bool = False) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO sync_states
            (sync_name, snapshot_cursor, page_cursor, etag, complete, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (sync_name, snapshot_cursor, page_cursor, etag, int(complete), utc_now()),
        )
        self.connection.commit()

    def sync_state(self, sync_name: str) -> Optional[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM sync_states WHERE sync_name = ?", (sync_name,)).fetchone()

    def verified_cases(self, run_id: str) -> Iterable[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM cases WHERE run_id = ? AND status = 'verified' ORDER BY case_id",
            (run_id,),
        ).fetchall()

    def save_content_version(self, version_id: str, run_id: str, body: str, content_hash: str) -> None:
        self.connection.execute(
            "INSERT INTO content_versions VALUES (?, ?, ?, ?, ?, ?)",
            (version_id, run_id, "frozen", body, content_hash, utc_now()),
        )
        self.connection.commit()

    def upsert_delivery_task(self, task_id: str, version_id: str, channel: str, target: str) -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO delivery_tasks
            (delivery_task_id, content_version_id, channel, target, status)
            VALUES (?, ?, ?, ?, 'pending')""",
            (task_id, version_id, channel, target),
        )
        self.connection.commit()

    def update_delivery(self, task_id: str, status: str, external_id: str = "", error: str = "") -> None:
        self.connection.execute(
            "UPDATE delivery_tasks SET status = ?, external_id = ?, last_error = ? WHERE delivery_task_id = ?",
            (status, external_id, error, task_id),
        )
        self.connection.commit()

    def receipt(self, task_id: str, status: str, details: str) -> None:
        self.connection.execute(
            "INSERT INTO delivery_receipts (delivery_task_id, status, details, created_at) VALUES (?, ?, ?, ?)",
            (task_id, status, details, utc_now()),
        )
        self.connection.commit()

    def counts(self, run_id: str) -> Dict[str, int]:
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM cases WHERE run_id = ? GROUP BY status", (run_id,)
        ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def events(self, run_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM run_events WHERE run_id = ? ORDER BY event_id", (run_id,)
        ).fetchall()

    def run(self, run_id: str) -> Optional[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    def cases(self, run_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM cases WHERE run_id = ? ORDER BY case_id", (run_id,)
        ).fetchall()

    def content_version(self, run_id: str) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM content_versions WHERE run_id = ? ORDER BY created_at DESC LIMIT 1", (run_id,)
        ).fetchone()

    def deliveries(self, run_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT delivery_tasks.* FROM delivery_tasks
            JOIN content_versions ON content_versions.content_version_id = delivery_tasks.content_version_id
            WHERE content_versions.run_id = ? ORDER BY delivery_tasks.delivery_task_id""",
            (run_id,),
        ).fetchall()


def normalize_url(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))
