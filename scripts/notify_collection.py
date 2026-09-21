#!/usr/bin/env python3
"""Preview/send a stored collection without repeating model calls or collection."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.config import runtime_dir
from backend.app.db import Database
from backend.app.notifications import notify_collection, preview_collection, retry_pending


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-id", help="Stored live collection ID")
    source.add_argument("--latest", action="store_true", help="Latest finished live collection")
    source.add_argument("--retry-pending", action="store_true", help="Retry known failed deliveries only")
    parser.add_argument("--send", action="store_true", help="Actually send; the default only previews")
    parser.add_argument("--test", action="store_true", help="Label the stored digest as an integration test")
    parser.add_argument("--quiet", action="store_true", help="Do not log empty or disabled retry checks")
    args = parser.parse_args()
    db = Database(runtime_dir() / "catalog.db")
    try:
        if args.retry_pending:
            if not args.send:
                parser.error('--retry-pending requires --send')
            output = retry_pending(db)
        else:
            result = db.collection_run(args.run_id) if args.run_id else next(
                (r for r in db.collection_history(1000)
                 if r.get('source_mode') == 'live' and r.get('status') in {'completed', 'partial', 'failed', 'interrupted'}), None)
            if not result:
                parser.error('No matching stored collection')
            if result.get('source_mode') != 'live' or result.get('status') not in {'completed', 'partial', 'failed', 'interrupted'}:
                parser.error('Only finished live collections can be notified')
            if args.test:
                result = {**result, 'notification_test': True}
            output = notify_collection(db, result) if args.send else preview_collection(db, result)
        entries = output if isinstance(output, list) else [output]
        if not args.quiet or any(x.get('status') != 'disabled' for x in entries):
            print(json.dumps(output, ensure_ascii=False, indent=2))
        if args.send and any(x.get('status') in {'failed', 'unknown', 'disabled'} for x in entries):
            raise SystemExit(1)
    finally:
        db.close()


if __name__ == '__main__':
    main()
