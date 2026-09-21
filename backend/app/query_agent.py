"""AIHOT-style read/query agent for the curated AI use-case catalog.

The query agent is deliberately read-only.  It turns the verified case
records collected by the workbench into a small, stable contract that can be
called by the website or another Agent.  Internet discovery will later plug
into the same catalog through Search/Fetch MCP; the query contract does not
need to change when that happens.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .scoring import SCORING_VERSION, score_case
from .db import Database
from .discovery_agent import SourceDiscoveryAgent
from .publication_dates import date_status, date_policy, parse_publication_date, today_local, with_publication_dates


WINDOW_DAYS = {"24h": 1, "7d": 7, "30d": 30, "1y": 365, "all": None}
CATEGORY_RULES = {
    "多 Agent": ("multi-agent", "multi agent", "多 agent", "多个代理", "agent team", "agent swarm", "project agent"),
    "工作流": ("workflow", "工作流", "自动化", "automation", "pipeline", "gitHub actions", "操作手册", "步骤", "流程"),
    "产品设计": ("产品经理", "产品设计", "product design", "user experience", "ux", "roadmap", "用户体验", "设计方案"),
    "个人效率": ("效率", "productivity", "节省", "耗时", "分钟", "小时", "日常", "写作", "文档", "会议"),
}


class QueryAgent:
    """Search and rank curated AI use-case records without mutating data."""

    name = "ai-case-query-agent"
    contract_version = "v1"

    def __init__(self, project_root: str | Path, catalog_db_path: str | Path | None = None) -> None:
        self.root = Path(project_root)
        self.source_discovery = SourceDiscoveryAgent(self.root)
        self.source_labels = {}
        catalog_path = self.root / "data/fixtures/source_catalog.json"
        if catalog_path.is_file():
            for item in json.loads(catalog_path.read_text(encoding="utf-8")):
                for value in [item.get("url", "")] + list(item.get("alternate_urls", [])):
                    host = urlparse(str(value)).hostname
                    if host:
                        self.source_labels[host.lower()] = str(item.get("name") or host)
        self.catalog_db = Database(catalog_db_path) if catalog_db_path else None
        if self.catalog_db:
            self.catalog_db.init_schema()

    def query(
        self,
        query: str = "",
        window: str = "7d",
        category: str | None = None,
        source_type: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
        sort: str = "relevance",
        source_mode: str = "online_snapshot",
        mode: str = "selected",
    ) -> dict[str, Any]:
        if window not in WINDOW_DAYS:
            raise ValueError(f"unsupported window: {window}")
        if sort not in {"relevance", "latest"}:
            raise ValueError(f"unsupported sort: {sort}")
        if source_mode not in {"live", "online_snapshot", "fixture", "all"}:
            raise ValueError(f"unsupported source mode: {source_mode}")

        if mode not in {"selected", "all"}:
            raise ValueError("unsupported selection mode")
        records = list(self._load_records(source_mode, include_candidates=mode == "all"))
        records = [record for record in records if self._is_publishable(record)]
        records = [record for record in records if self._within_window(record, window)]
        if category:
            records = [record for record in records if self._matches_category(record, category)]
        if source_type:
            records = [record for record in records if record.get("source_type") == source_type]

        normalized_query = query.strip().lower()
        ranked = [(self._score(record, normalized_query), record) for record in records]
        if normalized_query:
            ranked = [(score, record) for score, record in ranked if score > 0]
        ranked.sort(
            key=lambda item: (
                -item[0] if sort == "relevance" else 0,
                self._published_at(item[1]) * -1,
                item[1].get("case_id", ""),
            )
        )

        if not ranked and normalized_query and mode == "selected" and source_mode == "live":
            fallback = self.query(query, window, category, source_type, limit, cursor, sort, source_mode, mode="all")
            fallback["meta"]["fallback_to_candidates"] = bool(fallback["items"])
            fallback["meta"]["selection_note"] = "未找到匹配的精选；以下为已核验但未入选的候选" if fallback["items"] else "精选与候选均未找到"
            return fallback
        offset = self._decode_cursor(cursor)
        page = ranked[offset : offset + limit]
        next_cursor = self._encode_cursor(offset + limit) if offset + limit < len(ranked) else None
        return {
            "contract_version": self.contract_version,
            "agent": self.name,
            "query": query,
            "window": window,
            "filters": {"category": category, "source_type": source_type, "source_mode": source_mode, "mode": mode},
            "items": [self._public_item(record, score) for score, record in page],
            "next_cursor": next_cursor,
            "meta": {
                "total": len(ranked),
                "returned": len(page),
                "read_only": True,
                "data_status": "live_catalog" if source_mode == "live" else "curated_snapshot",
                "date_policy": date_policy(),
            },
        }

    def hot(self, limit: int = 10, source_mode: str = "online_snapshot") -> dict[str, Any]:
        result = self.query(window="all", limit=limit, sort="latest", source_mode=source_mode)
        result["meta"].update(ranking_kind="curated_recent", heat_available=False, label="近期精选，非热度榜")
        return result

    def digest(self, window: str = "7d", limit: int = 10, source_mode: str = "online_snapshot") -> dict[str, Any]:
        result = self.query(window=window, limit=limit, sort="latest", source_mode=source_mode)
        result["digest"] = {
            "title": f"AI 实际用法日报（{window}）",
            "generated_by": self.name,
            "selection_rule": "按原文发布日期倒序，保留最近一年内可回溯证据的案例",
        }
        return result

    def snapshot(self, cursor: str | None = None, limit: int = 50, source_mode: str = "online_snapshot") -> dict[str, Any]:
        result = self.query(window="all", limit=limit, cursor=cursor, sort="latest", source_mode=source_mode)
        result["sync"] = {
            "mode": "cursor_page",
            "change_types": [],
            "incremental_supported": False,
            "withdrawals": [],
        }
        return result

    def selected_snapshot(self, page: str | None = None, limit: int = 50, source_mode: str = "online_snapshot") -> dict[str, Any]:
        """Return a complete selected snapshot with separate page and sync cursors."""
        records = [record for record in self._load_records(source_mode) if self._is_publishable(record)]
        selected = [record for record in records if score_case(record)["selected"]]
        selected.sort(key=lambda record: (-self._published_at(record), record.get("case_id", "")))
        offset = self._decode_json_cursor(page, "page", source_mode)
        items = [self._public_item(record, 1) for record in selected[offset : offset + limit]]
        next_page = self._encode_json_cursor({"kind": "page", "source_mode": source_mode, "offset": offset + limit}) if offset + limit < len(selected) else None
        sync_cursor = self._encode_json_cursor({"kind": "sync", "source_mode": source_mode, "fingerprint": self._catalog_fingerprint(selected)})
        return {
            "schemaVersion": 1,
            "items": items,
            "cursor": sync_cursor,
            "page": {"count": len(items), "hasMore": next_page is not None, "nextPage": next_page},
            "meta": {"read_only": True, "data_status": "live_catalog" if source_mode == "live" else "curated_snapshot", "total": len(selected)},
        }

    def selected_changes(self, cursor: str, limit: int = 100, source_mode: str = "online_snapshot") -> dict[str, Any]:
        """Do not silently claim no changes when the live catalog changed."""
        fingerprint = self._decode_json_cursor(cursor, "sync", source_mode)
        current = self.selected_snapshot(limit=1, source_mode=source_mode)
        current_fingerprint = self._decode_json_cursor(current["cursor"], "sync", source_mode)
        return {"schemaVersion": 1, "changes": [], "cursor": cursor, "hasMore": False,
                "reset_required": fingerprint != current_fingerprint,
                "meta": {"read_only": True, "change_types": [], "incremental_supported": False, "sync_mode": "snapshot_reset", "returned": 0}}

    def _load_records(self, source_mode: str, include_candidates: bool = False) -> Iterable[dict[str, Any]]:
        paths: list[Path] = []
        if source_mode in {"online_snapshot", "all"}:
            paths.append(self.root / "data/fixtures/online_cases.json")
        if source_mode in {"fixture", "all"}:
            paths.append(self.root / "data/fixtures/cases.json")
        seen: set[str] = set()
        for path in paths:
            for record in json.loads(path.read_text(encoding="utf-8")):
                case_id = str(record.get("case_id", ""))
                if case_id and case_id not in seen:
                    seen.add(case_id)
                    yield record
        if self.catalog_db and source_mode in {"live", "all"}:
            for row in self.catalog_db.source_items(["selected", "featured", "candidate"] if include_candidates else ["selected", "featured"], 10000):
                record = json.loads(row["payload_json"])
                if record.get("discovery_mode") not in {"web_search", "rss", "douyin_browser"} or record.get("verification", {}).get("decision") != "pass":
                    continue
                if date_status(record.get('published_at')) != 'recent':
                    continue
                record = with_publication_dates(record)
                case_id = str(record.get("case_id", row["source_item_id"]))
                current_scorecard = score_case(record)
                if case_id not in seen and self._is_publishable(record) and (current_scorecard["selected"] or (include_candidates and current_scorecard["assessment_status"] == "approved")):
                    seen.add(case_id)
                    yield record

    @staticmethod
    def _is_publishable(record: dict[str, Any]) -> bool:
        # Demo fixtures use expected_label for deliberately bad examples.
        # Online records are treated as curated only after their three claim
        # blocks contain evidence.
        if record.get("expected_label") and record.get("expected_label") != "pass":
            return False
        return all(
            isinstance(record.get(field), dict)
            and record[field].get("claim")
            and record[field].get("evidence")
            for field in ("problem", "approach", "outcome")
        )

    @staticmethod
    def _within_window(record: dict[str, Any], window: str) -> bool:
        if window == '1y':
            return date_status(record.get('published_at')) == 'recent'
        days = WINDOW_DAYS[window]
        if days is None:
            return True
        published = QueryAgent._parse_date(record.get("published_at"))
        if published is None:
            return False
        today = today_local()
        return today - timedelta(days=days - 1) <= published <= today

    @staticmethod
    def _matches_category(record: dict[str, Any], category: str) -> bool:
        return category in QueryAgent._categories(record)

    @staticmethod
    def _categories(record: dict[str, Any]) -> list[str]:
        haystack = QueryAgent._search_text(record)
        categories = []
        for category, words in CATEGORY_RULES.items():
            if any(word.lower() in haystack for word in words):
                categories.append(category)
        return categories

    @staticmethod
    def _score(record: dict[str, Any], query: str) -> int:
        if not query:
            return 1
        fields = [
            str(record.get("title_original", "")),
            str(record.get("source_type", "")),
            str(record.get("published_at", "")),
            str(record.get("problem", {}).get("claim", "")),
            str(record.get("approach", {}).get("claim", "")),
            str(record.get("outcome", {}).get("claim", "")),
            " ".join(str(item) for item in record.get("limitations", [])),
        ]
        haystack = " ".join(fields).lower()
        tokens = [token for token in query.split() if token]
        return sum((3 if token in str(record.get("title_original", "")).lower() else 1) for token in tokens if token in haystack)

    @staticmethod
    def _search_text(record: dict[str, Any]) -> str:
        return " ".join(
            [
                str(record.get("title_original", "")),
                str(record.get("source_type", "")),
                str(record.get("problem", {}).get("claim", "")),
                str(record.get("approach", {}).get("claim", "")),
                str(record.get("outcome", {}).get("claim", "")),
            ]
        ).lower()

    @staticmethod
    def _published_at(record: dict[str, Any]) -> float:
        parsed = QueryAgent._parse_date(record.get("published_at"))
        return parsed.toordinal() if parsed else 0

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        return parse_publication_date(value)

    def _public_item(self, record: dict[str, Any], score: int) -> dict[str, Any]:
        scorecard = score_case(record)
        source_url = str(record.get("source_url", ""))
        source_meta = self.source_discovery.source_metadata(source_url)
        source_host = (urlparse(source_url).hostname or "").lower()
        recorded_source = str(record.get("source_name") or "")
        source_name = self.source_labels.get(source_host) if recorded_source.removeprefix("www.") == source_host.removeprefix("www.") else recorded_source
        source_name = source_name or source_host.removeprefix("www.") or "公开网页"
        if source_meta["source_class"] != "unknown_web":
            source_name = source_meta["source_name"]
        return {
            "id": record["case_id"],
            "title": record["title_original"],
            "summary": {
                "problem": record["problem"]["claim"],
                "approach": record["approach"]["claim"],
                "outcome": record["outcome"]["claim"],
            },
            "source_url": record["source_url"],
            "source_name": source_name,
            "source_type": record.get("source_type", "unknown"),
            "source_class": record.get("source_class") or source_meta["source_class"],
            "source_role": record.get("source_role") or source_meta["source_role"],
            "evidence_policy": record.get("evidence_policy") or source_meta["evidence_policy"],
            "source_catalog_id": record.get("source_catalog_id") or source_meta["source_catalog_id"],
            "categories": self._categories(record),
            "published_at": with_publication_dates(record)['published_at'],
            "published_at_source": record.get('published_at_source'),
            "published_at_evidence": record.get('published_at_evidence'),
            "updated_at": record.get('updated_at'),
            "date_status": date_status(record.get('published_at')),
            "collected_at": record.get("collected_at"),
            "source_verified_at": record.get("source_verified_at"),
            "limitations": record.get("limitations", []),
            "evidence": [record[field]["evidence"] for field in ("problem", "approach", "outcome")],
            "relevance_score": score,
            "quality_score": scorecard["quality_score"],
            "score_tier": scorecard["tier"],
            "selected": scorecard["selected"],
            "score_version": scorecard["score_version"],
            "score_reason": scorecard["reason"],
            "scorecard": scorecard,
        }

    @staticmethod
    def _encode_cursor(offset: int) -> str:
        return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None) -> int:
        if not cursor:
            return 0
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = int(base64.urlsafe_b64decode(padded).decode("ascii"))
            if value < 0:
                raise ValueError
            return value
        except (ValueError, UnicodeDecodeError, base64.binascii.Error) as exc:
            raise ValueError("invalid cursor") from exc

    @staticmethod
    def _encode_json_cursor(value: dict[str, Any]) -> str:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_json_cursor(cursor: str | None, kind: str, source_mode: str) -> int | str:
        if not cursor:
            return 0
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
            if value.get("kind") != kind or value.get("source_mode") != source_mode:
                raise ValueError("cursor does not belong to this query")
            return value.get("offset", value.get("fingerprint", ""))
        except (ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError, base64.binascii.Error) as exc:
            raise ValueError("invalid cursor") from exc

    @staticmethod
    def _catalog_fingerprint(records: list[dict[str, Any]]) -> str:
        value = json.dumps(records, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
