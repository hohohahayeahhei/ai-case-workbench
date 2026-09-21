"""Persistent discovery pipeline for the case catalog.

This pipeline keeps discovery separate from the existing publication workflow.
It can run entirely offline over verified snapshots, or accept explicit RSS
and Atom URLs.  Feed entries remain candidates until article evidence exists.
"""

from __future__ import annotations

import json
import re
import hashlib
import fcntl
import threading
import time
import sqlite3
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from .db import Database, normalize_url, utc_now
from .discovery_agent import SourceDiscoveryAgent
from .publication_dates import date_status, recent_cutoff, date_policy, with_publication_dates
from .extraction_agent import ExtractionAgent
from .agent_model import AgentModel, create_agent_model
from .scoring import score_case
from .quality_agent import QualityAgent
from .score_maintenance import reconcile_scores
from .agent_skills import skill_text, skill_versions
from .live_source import LiveSourceClient
from .search_cache import SearchCache
from .case_context import evidence_case_context
from .collection_health import PROCESSING_VERSION, reopen_fixed_failure, retry_ready, set_retry_state, prioritize_candidates, summarize_outcomes
from .douyin_source import DouyinStore, is_douyin_url


class CollectionPipeline:
    name = "case-collection-pipeline"

    def __init__(self, project_root: str | Path, catalog_db_path: str | Path, model: AgentModel | None = None) -> None:
        self.root = Path(project_root)
        self.db = Database(catalog_db_path)
        self.db.init_schema()
        reconcile_scores(self.db)
        self.discovery = SourceDiscoveryAgent(self.root)
        self.extractor = ExtractionAgent()
        self.model = model or create_agent_model()
        self.live_source = LiveSourceClient(self.root)
        self._lock = threading.Lock()
        self._active: dict[str, Any] | None = None
        self._douyin_store = None
        self._stage_clocks = {}

    @property
    def douyin(self):
        if self._douyin_store is None:
            self._douyin_store = DouyinStore(self.db.path)
        return self._douyin_store

    def close(self) -> None:
        if self._douyin_store is not None:
            self._douyin_store.close()
        self.db.close()

    def run(
        self,
        source_mode: str = "online_snapshot",
        query: str = "",
        feed_urls: Iterable[str] = (),
        include_catalog: bool = False,
        max_results: int = 12,
        search_web: bool = True,
        retry_pending: bool = False,
        run_id: str | None = None,
        retry_case_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        if source_mode not in {"live", "online_snapshot", "fixture", "all"} or not 1 <= max_results <= 20:
            raise ValueError("Unsupported source mode or collection limit")
        feed_urls = list(feed_urls)
        retry_ids = set(retry_case_ids or [])
        if retry_ids and (source_mode != 'live' or search_web or not retry_pending or include_catalog or feed_urls):
            raise ValueError('Targeted retry requires live, retry_pending, no search/catalog/feeds')
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Collection already running")
        lock_file = None
        try:
            if self.db.path != ":memory:":
                lock_file = open(self.db.path + ".lock", "a")
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = {"run_id": run_id or "collect-" + uuid4().hex[:12], "pipeline": self.name,
                      "pipeline_version": PROCESSING_VERSION,
                      "source_mode": source_mode, "query": query, "status": "running", "started_at": utc_now(),
                      "feed_urls": [], "feed_errors": [], "discovered": 0, "skipped": 0,
                      "processed_count": 0, "new_candidates": 0, "carried_over": 0, "counts": {}, "items": [], "events": [], "skill_versions": skill_versions(),
                      "acceptance": {"goal": "真实问题、AI方法和可观察结果有原文支持",
                         "publish_gate": "事实核验与独立质量复审通过，质量分达标",
                         "score_repair_limit": 1, "source_attempt_limit": 3,
                         "tool_boundary": "scout/fetcher使用只读搜索抓取MCP；抽取/核验/评分/复审只读原文；curator写本地数据库"}}
            result["date_policy"] = date_policy()
            result['source_skill_versions'] = {'douyin': skill_versions('douyin')}
            result['douyin'] = {'discovered': 0, 'ready': 0, 'processed': 0, 'items': [],
                                'browser_automation': False}
            if retry_ids:
                result['retry_case_ids'] = sorted(retry_ids)
            self._active = result
            self._stage_clocks.clear()
            self._event("orchestrator", "start", "running")
            result["processing_budget"] = max_results
            history_candidates = []
            # Live discovery must reserve capacity for existing work even when
            # a caller omitted the optional retry flag.
            if retry_pending or source_mode == 'live':
                for row in self.db.source_items(["needs_scoring", "needs_extraction", "needs_date"], 10000):
                    old = {**json.loads(row['payload_json']), 'status': row['status']}
                    if old.get('discovery_mode') == 'fixture' or (retry_ids and old.get('case_id') not in retry_ids):
                        continue
                    if old.get('discovery_mode') == 'douyin_browser':
                        continue  # Only the video adapter's current ready gate may admit it.
                    history_candidates.append(old)
            if source_mode == 'live':
                history_candidates.extend(self._ready_douyin_candidates(max_results, retry_ids))
                candidates = self._capacity_candidates(
                    result, history_candidates, max_results, query, feed_urls,
                    include_catalog, search_web, retry_pending)
            else:
                candidates = self._filter(self.discovery.fixture_candidates(source_mode), query)
                requested_feeds = list(feed_urls)
                if include_catalog and not (retry_pending and not search_web):
                    requested_feeds.extend(self.discovery.catalog_feed_urls())
                result["feed_urls"] = list(dict.fromkeys(requested_feeds))
                for url in result["feed_urls"]:
                    try:
                        candidates.extend(self.discovery.discover_feeds([url], query=query, max_results=15))
                    except Exception as exc:
                        result["feed_errors"].append({"url": url, "error": self._error(exc)})
                candidates.extend(history_candidates)
                result['carried_over'] = len(history_candidates)
            seen = set()
            pending = []
            for item in candidates:
                if is_douyin_url(item['source_url']) and item.get('discovery_mode') != 'douyin_browser':
                    self._queue_douyin(item['source_url'], query=query)
                    continue
                url = normalize_url(item["source_url"])
                if url in seen:
                    continue
                seen.add(url)
                previous = self.db.source_by_url(url)
                if previous and item.get('discovery_mode') == 'douyin_browser' and json.loads(previous['payload_json']).get('discovery_mode') != 'douyin_browser':
                    previous = self.db.source_item(item['case_id'])
                if previous and item.get("discovery_mode") != "fixture" and json.loads(previous["payload_json"]).get("discovery_mode") not in {"web_search", "rss", "douyin_browser"}:
                    previous = None  # Old snapshots must not bypass a real fetch and verification.
                if previous and item.get("discovery_mode") != "fixture":
                    old = json.loads(previous["payload_json"])
                    old['status'] = previous['status']
                    if previous['status'] in {'needs_extraction', 'needs_scoring', 'needs_date'} and reopen_fixed_failure(old):
                        self.db.update_source_item(old['case_id'], old['status'], old)
                    if previous["status"] not in {"needs_extraction", "needs_scoring", "needs_date"} or not retry_ready(old):
                        result["skipped"] += 1
                        continue
                    item = old
                item.setdefault("collected_at", utc_now())
                if item.get('discovery_mode') not in {'fixture', 'douyin_browser'}:
                    item.update(self.discovery.source_metadata(item['source_url']))
                if not previous:
                    self.db.save_source_item(item, status=self._initial_status(item))
                    result["new_candidates"] += 1
                if source_mode == 'live' and date_status(item.get('published_at')) == 'outdated':
                    item['status'] = 'outdated'
                    processed = self._process(item)
                    result['items'].append(processed)
                    result['counts']['outdated'] = result['counts'].get('outdated', 0) + 1
                    continue
                pending.append(item)
            if source_mode == 'live':
                pending = prioritize_candidates(pending)
            result['queue_policy'] = '优先完成已抓取待复审、实践者和官方案例；按域名轮转，避开冷却期和重试耗尽条目'
            attempt_limit = max_results if source_mode == 'live' else max_results * 3
            result['scheduled'] = [{'id': item['case_id'], 'title': item.get('title_original'), 'source_url': item['source_url']} for item in pending[:attempt_limit]]
            result['scheduled_count'] = len(result['scheduled']) + len(result['items'])
            # Historical queue size is not newly discovered content.
            result["discovered"] = result['new_candidates'] if source_mode == 'live' else len(seen)
            result["queued"] = result.get('deferred_history', 0) + len(pending)
            self._event("orchestrator", "discovery_complete", "completed", count=result['discovered'],
                        carried_over=result['carried_over'], scheduled=result['scheduled_count'])
            model_articles = 0
            consecutive_model_errors = 0
            processed_pending = 0
            for item in pending[:attempt_limit]:
                item_started = time.monotonic()
                item["attempt_count"] = item.get("attempt_count", 0) + 1
                if source_mode == 'live' and date_status(item.get('published_at')) != 'recent':
                    item = self._enrich_feed_item(item, use_mcp=True)
                elif item.get("status") == "needs_scoring":
                    cached = self.db.connection.execute("SELECT body FROM raw_documents WHERE document_id = ?", (item.get("raw_document_id", "doc-" + item["case_id"]),)).fetchone()
                    if cached:
                        feedback = self._claim_repair_feedback(item.get('quality_evaluation', {}))
                        if not feedback:
                            item["quality_evaluation"] = QualityAgent(self.model, self._event).evaluate(item, cached["body"])
                            feedback = self._claim_repair_feedback(item['quality_evaluation'])
                        if feedback:
                            item = self._enrich_feed_item(item, use_mcp=source_mode == 'live', review_feedback=feedback)
                    else:
                        item["status"] = "needs_extraction"
                        item = self._enrich_feed_item(item, use_mcp=source_mode == "live")
                elif item.get("status") in {"needs_extraction", "needs_date"}:
                    item = self._enrich_feed_item(item, use_mcp=source_mode == "live")
                if item.get('extraction', {}).get('extraction_status') not in {'fetch_failed', 'date_unavailable', 'outdated'}:
                    model_articles += 1
                processed = self._process(item)
                if item.get('douyin_item_id'):
                    self._finish_douyin_item(item, processed, item_started)
                diagnostic = processed.get('diagnostic', {})
                is_outage = diagnostic.get('code') == 'model_unavailable' or (
                    diagnostic.get('code') == 'quality_pending' and 'LLM request failed' in diagnostic.get('reason', ''))
                consecutive_model_errors = consecutive_model_errors + 1 if is_outage else 0
                result["items"].append(processed)
                processed_pending += 1
                result["processed_count"] = len(result["items"])
                result["queued"] = result.get("deferred_history", 0) + len(pending) - processed_pending
                status = processed["status"]
                result["counts"][status] = result["counts"].get(status, 0) + 1
                self._event("curator", "persist", status, case_id=item["case_id"])
                if consecutive_model_errors >= 2:
                    result['stop_reason'] = 'model_service_unavailable'
                    self._event('orchestrator', 'model.circuit_open', 'partial', reason='连续两篇模型服务失败；保留其余队列，服务恢复后重试')
                    break
                if model_articles >= max_results:
                    break
            result['diagnostics'] = summarize_outcomes(result['items'])
            result["processed_count"] = len(result["items"])
            result["queued"] = result.get("deferred_history", 0) + max(0, len(pending) - processed_pending)
            if result["queued"] and not result.get('stop_reason'):
                result['stop_reason'] = 'processing_budget_reached'
            failures = bool(result["queued"]) or bool(result["feed_errors"]) or bool(result["counts"].get("needs_extraction")) or bool(result["counts"].get("needs_scoring")) or bool(result["counts"].get("needs_date"))
            result["status"] = "partial" if failures and seen else "failed" if failures else "completed"
            result["finished_at"] = utc_now()
            self.db.save_sync_state("collection", complete=result["status"] == "completed")
            self._event("orchestrator", "finish", result["status"])
            return result
        except Exception as exc:
            if self._active:
                self._active.update(status="failed", error=self._error(exc), finished_at=utc_now())
                self.db.save_collection_run(self._active)
            raise
        finally:
            self._active = None
            if lock_file:
                lock_file.close()
            self._lock.release()

    def _capacity_candidates(self, result: dict, history: list[dict], budget: int,
                             query: str, feed_urls: list[str], include_catalog: bool,
                             search_web: bool, retry_pending: bool) -> list[dict]:
        """Reserve history first; every source shares one bounded discovery budget.

        Requested result slots, not successful results, consume the discovery
        allowance. Duplicates/empty sources therefore cannot trigger an unbounded
        refill search, and an over-producing adapter cannot grow the queue.
        """
        ready = []
        history_urls = set()
        for old in history:
            probe = {**old, 'attempt_history': list(old.get('attempt_history', []))}
            reopen_fixed_failure(probe)
            url = normalize_url(probe['source_url'])
            if retry_ready(probe) and url not in history_urls:
                history_urls.add(url)
                if probe.get('discovery_mode') != 'douyin_browser':
                    probe.update(self.discovery.source_metadata(url))
                ready.append(probe)
        result['skipped'] += len(history) - len(ready)
        ready = prioritize_candidates(ready)
        videos = [item for item in ready if item.get('discovery_mode') == 'douyin_browser']
        ordinary = [item for item in ready if item.get('discovery_mode') != 'douyin_browser']
        # A small admitted video batch must not starve behind a large article backlog.
        reserved = prioritize_candidates([*videos[:budget], *ordinary[:max(0, budget - len(videos))]])
        allowance = max(0, budget - len(reserved))
        result.update(discovery_policy='capacity_matched', backlog_at_start=len(history),
                      ready_at_start=len(ready), carried_over=len(reserved),
                      deferred_history=max(0, len(ready) - len(reserved)),
                      discovery_budget=allowance, discovery_requested=0,
                      source_results=0)
        self._event('orchestrator', 'discovery.capacity', 'completed',
                    processing_budget=budget, reserved_history=len(reserved),
                    discovery_budget=allowance,
                    reason=f'本轮最多处理 {budget} 条，历史占用 {len(reserved)} 条，只补搜最多 {allowance} 条')
        if not allowance:
            self._event('scout', 'discovery.paused', 'completed', reason='历史待处理已占满本轮名额，不新增搜索或 RSS 线索')
            return reserved

        requested_feeds = list(feed_urls)
        if include_catalog and not (retry_pending and not search_web):
            requested_feeds.extend(self.discovery.catalog_feed_urls())
        requested_feeds = list(dict.fromkeys(requested_feeds))
        plan = []
        if search_web:
            topic = query or 'first person how I use AI tools to solve everyday and work problems practical workflow original case study'
            topic += f" 原文发布日期在 {recent_cutoff().isoformat()} 至 {date_policy()['until']} 之间（含首尾），优先最新实践；只用发布日期，不用页面更新时间或收录时间"
            branches = self.discovery.search_branches(topic) if include_catalog else []
            plan = [('web', 'general', topic), *[('web', b['id'], b['query']) for b in branches]]
            result['search_plan'] = {
                'topic': topic, 'branches': ['亲历者/实践文章'] + [b['label'] for b in branches],
                'source_policy': '官方可作为一手证据；官方社交、媒体和分析站只作线索，必须回溯原始长文或案例',
                'acceptance': '原文包含实际任务、AI步骤、观察到的结果和最近一年内的发布日期',
                'discovery_budget': allowance}
        plan.extend(('rss', url, url) for url in requested_feeds)
        cache = SearchCache(Path(self.db.path).parent / 'search-cache') if search_web else None
        remaining = allowance
        candidates = list(reserved)
        seen = set(history_urls)
        search_errors = 0
        for index, (kind, branch_id, value) in enumerate(plan):
            if remaining <= 0:
                break
            if kind == 'web' and search_errors >= 2:
                continue
            # Share slots across enabled sources, never a separate per-source cap.
            limit = (remaining + len(plan) - index - 1) // (len(plan) - index)
            remaining -= limit
            result['discovery_requested'] += limit
            action = ('mcp.search_web' if branch_id == 'general' else f'mcp.search_web.{branch_id}') if kind == 'web' else 'rss.discover'
            self._event('scout', action, 'running', requested=limit)
            try:
                from_cache = False
                if kind == 'web':
                    batch = cache.get(value, limit)
                    from_cache = batch is not None
                    if batch is None:
                        batch = self.live_source.call('search_web', query=value, max_results=limit)[:limit]
                        cache.put(value, limit, batch)
                    search_errors = 0
                else:
                    result['feed_urls'].append(value)
                    batch = self.discovery.discover_feeds([value], query=query, max_results=limit)
                batch = batch[:limit]
                result['source_results'] += len(batch)
                for source in batch:
                    url = normalize_url(source['url'] if kind == 'web' else source['source_url'])
                    if is_douyin_url(url):
                        self._queue_douyin(url, query=(query or value) if kind == 'web' else query)
                        continue
                    previous = self.db.source_by_url(url)
                    if url in seen or (previous and json.loads(previous['payload_json']).get('discovery_mode') in {'web_search', 'rss'}):
                        result['skipped'] += 1
                        continue
                    seen.add(url)
                    if kind == 'web':
                        item = {'case_id': 'web-' + hashlib.sha256(url.encode()).hexdigest()[:16],
                                'source_url': url, 'title_original': source.get('title') or url,
                                **self.discovery.source_metadata(url), 'source_type': 'web',
                                'discovery_mode': 'web_search', 'status': 'needs_extraction',
                                'search_provider': source.get('provider'), 'search_branch': branch_id,
                                'search_published_at_hint': source.get('published_at') or source.get('date'),
                                'collected_at': utc_now()}
                    else:
                        item = dict(source)
                    candidates.append(item)
                self._event('scout', action, 'completed', count=len(batch), from_cache=from_cache)
            except Exception as exc:
                result['feed_errors'].append({'url': f'web_search:{branch_id}' if kind == 'web' else value,
                                              'error': self._error(exc)})
                self._event('scout', action, 'failed', error=self._error(exc))
                if kind == 'web':
                    search_errors += 1
                    if search_errors >= 2:
                        self._event('scout', 'search.circuit_open', 'partial', reason='连续两次搜索失败，本轮先处理已发现及历史候选')
        return candidates

    def _queue_douyin(self, url: str, query: str = '') -> None:
        """A search hit is a browser task, never an article made from its title."""
        try:
            record = self.douyin.import_link(url, query=query)
            details = {'item_id': record['item_id'], 'status': record['status'],
                       'source_url': record.get('canonical_url') or record.get('source_url') or url}
            if self._active is not None:
                items = self._active['douyin']['items']
                if not any(old['item_id'] == record['item_id'] for old in items):
                    items.append(details)
                    self._active['douyin']['discovered'] += 1
            self._event('fetcher', 'douyin.browser_queue', record['status'],
                        item_id=record['item_id'], source_url=details['source_url'])
            # Migrate a previously misrouted HTML candidate without losing its raw data.
            previous = self.db.source_by_url(url)
            if previous and previous['status'] in {'needs_extraction', 'needs_date', 'needs_scoring'}:
                old = json.loads(previous['payload_json'])
                old.update(status='awaiting_browser', douyin_item_id=record['item_id'])
                self.db.update_source_item(old['case_id'], 'awaiting_browser', old)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            self._event('fetcher', 'douyin.browser_queue', 'failed', source_url=url, error=self._error(exc))

    def _ready_douyin_candidates(self, budget: int, retry_ids: set[str]) -> list[dict]:
        """Share the normal model budget; unreadable video tasks consume no model slot."""
        batch = []
        limit = min(3, max(1, (budget + 2) // 3))
        try:
            if retry_ids:
                records = []
                for target in sorted(retry_ids):
                    if not target.startswith('douyin-'):
                        continue
                    try:
                        record = self.douyin.get_item('douyin:' + target.removeprefix('douyin-'))
                    except KeyError:
                        continue
                    if record['status'] == 'ready' and self.douyin.evidence_document(record['item_id']).get('ready'):
                        records.append(record)
                    if len(records) >= limit:
                        break
            else:
                records = self.douyin.ready_items(limit=20)
            for record in records:
                item_id = record['item_id']
                case_id = 'douyin-' + item_id.split(':', 1)[-1]
                if retry_ids and case_id not in retry_ids:
                    continue
                previous = self.db.source_item(case_id)
                if previous:
                    old = {**json.loads(previous['payload_json']), 'status': previous['status']}
                    if old.get('douyin_evidence_revision') != self._douyin_revision(record):
                        old.update(status='needs_extraction', attempt_count=0,
                                   source_url=record.get('canonical_url') or record['source_url'],
                                   published_at=record.get('published_at'),
                                   published_at_source=record.get('published_at_source'),
                                   published_at_evidence=record.get('published_at_evidence'),
                                   author=record.get('author', ''), title_original=record.get('title') or old.get('title_original'))
                        old.pop('next_retry_at', None)
                        self.db.update_source_item(case_id, 'needs_extraction', old)
                    if old['status'] not in {'needs_extraction', 'needs_scoring', 'needs_date'} or not retry_ready(old):
                        if old['status'] in {'selected', 'featured', 'candidate', 'rejected', 'outdated'}:
                            self.douyin.mark_processed(item_id, case_id, status=old['status'])
                        elif old['status'] == 'evidence_insufficient':
                            self.douyin.set_status(item_id, 'evidence_insufficient', reason=old.get('extraction', {}).get('reason', ''))
                        continue
                    batch.append(old)
                else:
                    batch.append({'case_id': case_id, 'douyin_item_id': item_id,
                        'source_url': record.get('canonical_url') or record['source_url'],
                        'title_original': record.get('title') or record.get('canonical_url'),
                        'author': record.get('author', ''), 'source_name': '抖音',
                        'source_type': 'douyin', 'source_class': 'practitioner',
                        'discovery_mode': 'douyin_browser', 'status': 'needs_extraction',
                        'published_at': record.get('published_at'),
                        'published_at_source': record.get('published_at_source'),
                        'published_at_evidence': record.get('published_at_evidence'),
                        'collected_at': record.get('collected_at') or utc_now()})
                if len(batch) >= limit:
                    break
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            self._event('fetcher', 'douyin.ready_queue', 'failed', error=self._error(exc))
        if self._active is not None:
            self._active['douyin']['ready'] = len(batch)
        return batch

    def _finish_douyin_item(self, item: dict, processed: dict, started: float) -> None:
        status = processed['status']
        try:
            if status in {'selected', 'featured', 'candidate', 'rejected', 'outdated'}:
                self.douyin.mark_processed(item['douyin_item_id'], item['case_id'], status=status)
            elif status == 'evidence_insufficient':
                self.douyin.set_status(item['douyin_item_id'], 'evidence_insufficient',
                    reason=processed.get('diagnostic', {}).get('reason', ''), stage='verification',
                    duration_ms=round((time.monotonic() - started) * 1000))
            # Transient model errors remain ready; source_items provides bounded retries/cooldown.
            if self._active is not None:
                self._active['douyin']['processed'] += 1
            self._event('curator', 'douyin.collection', status, case_id=item['case_id'],
                        duration_ms=round((time.monotonic() - started) * 1000))
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            self._event('curator', 'douyin.collection', 'failed', case_id=item['case_id'], error=self._error(exc))

    @staticmethod
    def _douyin_revision(record: dict) -> str:
        facts = {key: record.get(key) for key in ('canonical_url', 'published_at', 'published_at_source', 'published_at_evidence', 'author', 'title')}
        facts['evidence'] = sorted([
            {key: value for key, value in segment.items() if key not in {'observed_at', 'evidence_id'}}
            for segment in record.get('evidence', [])], key=lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True))
        return hashlib.sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _bind_douyin_evidence(item: dict, evidence: list[dict]) -> bool:
        """Keep the video locations attached to exact quotes; oral-only outcomes cannot pass."""
        compact = lambda value: re.sub(r'\s+', '', str(value))
        for field in ('problem', 'approach', 'outcome'):
            block = item.get(field, {})
            quote = compact(block.get('evidence', ''))
            block['video_evidence'] = [dict(segment) for segment in evidence
                if quote and len(compact(segment.get('text', ''))) >= 8
                and (compact(segment['text']) in quote or quote in compact(segment['text']))]
        return all(item.get(field, {}).get('video_evidence') for field in ('problem', 'approach', 'outcome')) and any(segment.get('kind') in {'visible_frame', 'external_corroboration'}
                   and segment.get('role') == 'outcome'
                   and (not segment.get('machine_generated') or segment.get('verified'))
                   for segment in item.get('outcome', {}).get('video_evidence', []))

    @staticmethod
    def _error(exc: Exception) -> str:
        return f"{type(exc).__name__}: {str(exc)[:240]}"

    def _event(self, agent: str, action: str, status: str, **details) -> None:
        if self._active is not None:
            key = (agent, action, details.get('case_id'))
            if status == 'running':
                self._stage_clocks[key] = time.monotonic()
            elif key in self._stage_clocks:
                details.setdefault('duration_ms', round((time.monotonic() - self._stage_clocks.pop(key)) * 1000))
            self._active["events"].append({"event_id": f"{self._active['run_id']}:{len(self._active['events'])}", "agent": agent, "action": action, "status": status, "at": utc_now(), **details})
            self.db.save_collection_run(self._active)

    @staticmethod
    def _claim_repair_feedback(evaluation: dict) -> dict | None:
        if evaluation.get('status') != 'pending':
            return None
        review = next((attempt['review'] for attempt in reversed(evaluation.get('attempts', [])) if attempt.get('review')), {})
        layers = review.get('editorial_checks', {})
        if isinstance(layers, dict) and any(layers.get(key) is False for key in ('factual', 'attribution')):
            return review
        return None

    def _enrich_feed_item(self, item: dict[str, Any], use_mcp: bool = False, review_feedback: dict | None = None) -> dict[str, Any]:
        if review_feedback:
            item = dict(item)
            item['claim_repair_history'] = [*item.get('claim_repair_history', []),
                {'at': utc_now(), 'review': review_feedback, 'previous_claims': {key: item.get(key) for key in ('problem', 'approach', 'outcome')}}]
            self._event('extractor', 'claims.repair', 'running', case_id=item['case_id'])
        # No stale verification or approval may authorize newly extracted claims.
        item = {key: value for key, value in item.items() if key not in {'quality_evaluation', 'scorecard', 'verification', 'source_verified_at'}}
        item['status'] = 'needs_extraction'
        try:
            document_id = "doc-" + item["case_id"]
            cached = self.db.connection.execute("SELECT * FROM raw_documents WHERE document_id = ?", (document_id,)).fetchone()
            is_video = item.get('discovery_mode') == 'douyin_browser'
            action = 'douyin.read_evidence' if is_video else 'mcp.fetch_page' if use_mcp else 'fetch_page'
            self._event("fetcher", action, "running", case_id=item["case_id"])
            cached_metadata = json.loads(cached['metadata_json']) if cached else {}
            # Legacy snapshots did not save headers. Fetch once to recover a date.
            if is_video:
                page = self.douyin.evidence_document(item['douyin_item_id'])
                item.update(douyin_record=page.get('douyin_record', {}),
                            video_evidence=page.get('evidence', []), source_type='douyin',
                            douyin_evidence_revision=self._douyin_revision(page.get('douyin_record', {})))
            elif is_douyin_url(item['source_url']):
                self._queue_douyin(item['source_url'])
                return {**item, 'status': 'awaiting_browser', 'extraction': {
                    'extraction_status': 'awaiting_browser', 'reason': '抖音须在本地浏览器观察或导入素材，标题不是正文证据。'}}
            elif cached and not self.discovery._access_page(cached['body'], cached['title']) and date_status(item.get('published_at') or cached_metadata.get('published_at')) == 'recent':
                page = {"text": cached["body"], "content_hash": cached["content_hash"], "title": cached["title"], **cached_metadata}
            else:
                page = self.live_source.call("fetch_page", url=item["source_url"]) if use_mcp else self.discovery.fetch_page(item["source_url"])
            for key in ('published_at', 'published_at_source', 'published_at_evidence', 'updated_at', 'updated_at_source', 'updated_at_evidence'):
                if page.get(key):
                    item[key] = page[key]
            item = with_publication_dates(item)
            item['raw_document_id'] = document_id
            self.db.save_raw_document({**page, "document_id": document_id, "source_item_id": item["case_id"], "source_url": item["source_url"]})
            self._event('fetcher', action, 'completed', case_id=item['case_id'])
            self._event('fetcher', 'fetch.complete', 'completed', case_id=item['case_id'], characters=len(page['text']))
            if use_mcp and item['date_status'] != 'recent':
                item['status'] = 'outdated' if item['date_status'] == 'outdated' else 'needs_date'
                item['extraction'] = {'extraction_status': 'outdated' if item['status'] == 'outdated' else 'date_unavailable',
                                      'reason': '原文发布时间不在最近一年范围内' if item['status'] == 'outdated' else '原文发布日期缺失、无效或晚于今天；等待日期证据，收录与更新时间不能替代。'}
                self._event('verifier', 'date.filter', item['status'], case_id=item['case_id'], published_at=item.get('published_at'))
                return item
            if is_video and page.get('ready') is False:
                item.update(status='evidence_insufficient', extraction={
                    'extraction_status': 'evidence_insufficient', 'reason': page.get('reason') or '当前视频证据不满足抽取门槛'})
                self._event('verifier', 'douyin.evidence_gate', 'evidence_insufficient', case_id=item['case_id'])
                return item
            try:
                self._event("extractor", "model.extraction", "running", case_id=item["case_id"])
                extraction = self.extractor.extract_case(item, page, self.model, review_feedback=review_feedback)
                self._event("extractor", "model.extraction", extraction.get("extraction_status", "unknown"), case_id=item["case_id"])
            except (RuntimeError, ValueError, OSError) as exc:
                extraction = {
                    "extraction_status": "model_unavailable",
                    "signals": self.extractor.extract_signals(item, page),
                    "reason": f"模型服务暂不可用，保留候选等待重试：{exc}",
                }
                self._event("extractor", "model.extraction", "failed", case_id=item["case_id"], error=self._error(exc))
            enriched = {**item, "raw_document_id": document_id, "article_hash": hashlib.sha256(page["text"].encode()).hexdigest(), "extraction": extraction,
                        "fetch_strategy": page.get("fetch_strategy", "direct"),
                        "fetch_strategy_label": page.get("fetch_strategy_label", "普通公开网页"),
                        "fetched_url": page.get("fetched_url", item["source_url"])}
            if extraction.get('extraction_status') == 'not_case':
                enriched.update(status='rejected', rejection_stage='extraction')
                enriched['verification'] = {'decision': 'reject', 'reason': extraction['reason']}
                return enriched
            if enriched.get("title_original", "").startswith(("http://", "https://")):
                enriched["title_original"] = page.get("title") or enriched["title_original"]
            if extraction.get("extraction_status") == "structured_claims":
                enriched.update({field: extraction[field] for field in ("problem", "approach", "outcome")})
                enriched["limitations"] = extraction.get("limitations", [])
                if is_video and not self._bind_douyin_evidence(enriched, page.get('evidence', [])):
                    enriched.update(status='evidence_insufficient', extraction={
                        'extraction_status': 'evidence_insufficient',
                        'reason': '结果引文没有对应时间段的可见结果画面或外部印证；口述/机器转写不能单独证明效果。'})
                    self._event('verifier', 'douyin.evidence_gate', 'evidence_insufficient', case_id=item['case_id'])
                    return enriched
                self._event("verifier", "model.verification", "running", case_id=item["case_id"])
                try:
                    review = self.model.structured("verification", skill_text("verify", enriched.get('source_type')), {"case": evidence_case_context(enriched), "article_text": page["text"]})
                except (RuntimeError, ValueError, OSError) as exc:
                    enriched["extraction"] = {"extraction_status": "verification_unavailable", "reason": self._error(exc)}
                    self._event("verifier", "model.verification", "failed", case_id=item["case_id"], error=self._error(exc))
                    return enriched
                enriched["verification"] = {"decision": review.get("decision", "reject"), "reason": str(review.get("reason", "未给出核验依据"))}
                self._event("verifier", "model.verification", enriched["verification"]["decision"], case_id=item["case_id"])
                if review.get("decision") != "pass":
                    enriched["status"] = "rejected"
                    return enriched
                if isinstance(review.get("limitations"), list):
                    enriched["limitations"] = list(dict.fromkeys(enriched["limitations"] + [str(x) for x in review["limitations"]]))
                enriched["source_verified_at"] = utc_now()
                enriched["status"] = "candidate"
                enriched["quality_evaluation"] = QualityAgent(self.model, self._event).evaluate(enriched, page["text"])
                feedback = self._claim_repair_feedback(enriched['quality_evaluation'])
                if feedback and not review_feedback:
                    return self._enrich_feed_item(enriched, use_mcp=use_mcp, review_feedback=feedback)
            return enriched
        except (OSError, ValueError, UnicodeError, RuntimeError, sqlite3.Error) as exc:
            self._event('fetcher', 'fetch.failed', 'failed', case_id=item['case_id'], error=self._error(exc))
            return {**item, "extraction": {"extraction_status": "fetch_failed", "reason": str(exc)}}

    def _process(self, item: dict[str, Any]) -> dict[str, Any]:
        item = with_publication_dates(item)
        if item.get('status') in {'awaiting_browser', 'evidence_insufficient'}:
            set_retry_state(item)
            item['diagnostic'] = {'code': item['status'],
                'label': '等待本地浏览器读取' if item['status'] == 'awaiting_browser' else '视频结果证据不足',
                'reason': item.get('extraction', {}).get('reason', '')}
            self.db.update_source_item(item['case_id'], item['status'], item)
            return {'id': item['case_id'], 'status': item['status'], 'title': item.get('title_original', ''), 'diagnostic': item['diagnostic']}
        if item.get('status') in {'selected', 'featured', 'candidate', 'needs_scoring'} and item.get('discovery_mode') in {'web_search', 'rss', 'douyin_browser'} and item['date_status'] != 'recent':
            item['status'] = 'outdated' if item['date_status'] == 'outdated' else 'needs_date'
        if item.get('status') in {'outdated', 'needs_date'}:
            set_retry_state(item)
            self.db.update_source_item(item['case_id'], item['status'], item)
            return {'id': item['case_id'], 'status': item['status'], 'title': item.get('title_original', ''), 'diagnostic': item['diagnostic']}
        if item.get("status") == "rejected":
            set_retry_state(item)
            self.db.update_source_item(item["case_id"], "rejected", item)
            return {"id": item["case_id"], "status": "rejected", "title": item.get("title_original", ""), "diagnostic": item['diagnostic'], "reason": item.get("verification", {}).get("reason", "核验未通过")}
        if item.get("status") == "needs_extraction" or not self._has_case_evidence(item):
            item['status'] = 'needs_extraction'
            set_retry_state(item)
            self.db.update_source_item(item["case_id"], "needs_extraction", item)
            return {"id": item["case_id"], "status": "needs_extraction", "title": item.get("title_original", ""), 'diagnostic': item['diagnostic']}
        cluster_id = self._cluster_id(item)
        scorecard = score_case(item, item.get("model_scorecard"))
        status = "selected" if scorecard["selected"] else scorecard["tier"]
        item = {**item, 'status': status, 'scorecard': scorecard, 'cluster_id': cluster_id}
        set_retry_state(item)
        self.db.update_source_item(item['case_id'], status, item)
        self.db.save_cluster(cluster_id, item["case_id"], item["title_original"], [item["case_id"]])
        return {
            "id": item["case_id"],
            "status": status,
            "title": item.get("title_original", ""),
            "quality_score": scorecard["quality_score"],
            "score_tier": scorecard["tier"],
            "cluster_id": cluster_id,
            "diagnostic": item['diagnostic'],
        }

    @staticmethod
    def _filter(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        if not query.strip():
            return items
        tokens = [token.lower() for token in query.split() if token]
        return [item for item in items if any(token in json.dumps(item, ensure_ascii=False).lower() for token in tokens)]

    @staticmethod
    def _initial_status(item: dict[str, Any]) -> str:
        return "needs_extraction" if item.get("status") == "needs_extraction" else "candidate"

    @staticmethod
    def _has_case_evidence(item: dict[str, Any]) -> bool:
        return all(
            isinstance(item.get(field), dict)
            and item[field].get("claim")
            and item[field].get("evidence")
            for field in ("problem", "approach", "outcome")
        )

    @staticmethod
    def _cluster_id(item: dict[str, Any]) -> str:
        title = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(item.get("title_original", "")).lower())
        return "cluster-" + __import__("hashlib").sha1(title.encode("utf-8")).hexdigest()[:12]
