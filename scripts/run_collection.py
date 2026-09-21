#!/usr/bin/env python3
"""Run one local collection cycle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.collection_pipeline import CollectionPipeline
from backend.app.collection_export import export_collection
from backend.app.config import runtime_dir
from backend.app.db import Database, utc_now
from backend.app.notifications import notification_configuration, notify_collection, retry_pending


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AI case discovery and curation")
    parser.add_argument("--source-mode", choices=["live", "online_snapshot", "fixture", "all"], default="live")
    parser.add_argument("--query", default="")
    parser.add_argument("--feed", action="append", default=[], help="Explicit RSS/Atom URL; repeatable")
    parser.add_argument("--catalog", action="store_true", help="Read enabled public feeds from source_catalog.json")
    parser.add_argument("--max-results", type=int, default=12, help="Bound the number of articles processed (1-20)")
    parser.add_argument("--retry-pending", action="store_true")
    parser.add_argument("--case-id", action="append", default=[], help="Restrict retry to these pending IDs; requires --retry-pending --no-search and no feeds/catalog")
    parser.add_argument("--no-search", action="store_true", help="Retry pending candidates or use RSS without a new web search")
    parser.add_argument("--notify", action="store_true", help="Send the completed live collection digest to the configured channel")
    parser.add_argument("--check-setup", action="store_true", help="Validate local runtime and notification configuration without collecting or sending")
    args = parser.parse_args()
    if args.case_id and (args.source_mode != 'live' or not args.retry_pending or not args.no_search or args.feed or args.catalog):
        parser.error('--case-id requires live --retry-pending --no-search, without --feed or --catalog')
    if args.notify and args.source_mode != 'live':
        parser.error('--notify requires --source-mode live')
    data_dir = runtime_dir()
    if args.check_setup:
        db = Database(data_dir / 'catalog.db')
        try:
            if db.connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Catalog integrity check failed')
            configuration = notification_configuration(data_dir)
            if args.notify and not configuration['enabled']:
                parser.error('Daily notifications are disabled')
            if configuration['enabled'] and not configuration['configured']:
                parser.error(configuration.get('error', 'Invalid notification configuration'))
            print(json.dumps({'status': 'ready', 'notifications_enabled': configuration['enabled'],
                              'notification_provider': configuration.get('provider'),
                              'data_dir': str(data_dir)}, ensure_ascii=False))
        finally:
            db.close()
        return
    pipeline = CollectionPipeline(ROOT, data_dir / "catalog.db")
    run_id = "collect-" + uuid4().hex[:12]
    try:
        try:
            result = pipeline.run(args.source_mode, args.query, args.feed, args.catalog,
                                  args.max_results, not args.no_search, args.retry_pending,
                                  run_id=run_id, retry_case_ids=args.case_id)
            result["artifacts"] = export_collection(pipeline.db, result, data_dir / "exports")
        except Exception as exc:
            result = pipeline.db.collection_run(run_id) or {
                "run_id": run_id, "source_mode": args.source_mode, "items": [], "started_at": utc_now()}
            result.update(status="failed", finished_at=utc_now(), error=type(exc).__name__)
        pipeline.db.save_collection_run(result)
        if args.notify:
            # Notification errors must not change collection status or skip backup.
            try:
                retry_pending(pipeline.db)
                result["notification"] = notify_collection(pipeline.db, result)
            except Exception:
                result["notification"] = {"status": "failed", "error": "notification_internal_error"}
            pipeline.db.save_collection_run(result)
    finally:
        pipeline.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
