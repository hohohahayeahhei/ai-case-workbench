"""Recover publication dates without changing stored claims, scores or source bodies."""
import argparse
import fcntl
import json
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.config import runtime_dir
from backend.app.db import Database, utc_now
from backend.app.discovery_agent import SourceDiscoveryAgent
from backend.app.publication_dates import publication_metadata, with_publication_dates, temporal_view


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-network', action='store_true', help='Read original public articles with missing dates')
    parser.add_argument('--fetch-limit', type=int, default=80)
    args = parser.parse_args()
    directory = runtime_dir()
    with open(directory/'catalog.db.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        backup = directory/'backups'/('before-publication-dates-'+utc_now().replace(':','-')+'.db')
        backup.parent.mkdir(parents=True, exist_ok=True)
        db = Database(directory/'catalog.db')
        try:
            with sqlite3.connect(backup) as destination:
                db.connection.backup(destination)
            db.init_schema()
            records = []
            for row in db.source_items(limit=10000):
                item = json.loads(row['payload_json'])
                if item.get('discovery_mode') not in {'web_search','rss'}:
                    continue
                item['status'] = row['status']
                if item.get('published_at') and not item.get('published_at_source'):
                    item['published_at_evidence'] = item['published_at']
                    item['published_at_source'] = 'legacy_feed:published' if item.get('source_type') == 'rss' else 'legacy_record'
                cached = db.connection.execute('SELECT * FROM raw_documents WHERE source_item_id=? ORDER BY fetched_at DESC LIMIT 1', (row['source_item_id'],)).fetchone()
                if cached and not item.get('published_at'):
                    metadata = json.loads(cached['metadata_json']) or publication_metadata(cached['body'], 'text/plain')
                    item.update(metadata)
                item = with_publication_dates(item)
                records.append((row['source_item_id'], item, bool(cached)))
            ranked = sorted(records, key=lambda entry: (0 if entry[1]['status'] in {'selected','featured'} else 1 if entry[1]['status'] in {'candidate','needs_scoring'} else 2, entry[0]))
            targets = [(key,item) for key,item,cached in ranked if not item.get('published_at') and cached][:max(0,args.fetch_limit)] if args.allow_network else []
            failures = []
            recovered = 0
            def fetch(key, item):
                try:
                    page = SourceDiscoveryAgent(ROOT, timeout=10).fetch_page(item['source_url'])
                    metadata = {name:value for name,value in page.items() if name.startswith(('published_at','updated_at'))}
                    return key, metadata, None
                except Exception as exc:
                    return key, {}, f'{type(exc).__name__}: {str(exc)[:200]}'
            mapping = {key:item for key,item,_ in records}
            with ThreadPoolExecutor(max_workers=4) as pool:
                tasks = [pool.submit(fetch,key,item) for key,item in targets]
                for completed, future in enumerate(as_completed(tasks), 1):
                    key, metadata, error = future.result()
                    mapping[key].update(metadata)
                    mapping[key]['date_checked_at'] = utc_now()
                    if error:
                        failures.append({'id':key,'error':error})
                    recovered += bool(metadata.get('published_at'))
                    print(f'Date backfill {completed}/{len(tasks)}; recovered={recovered}', flush=True)
            for key,item,_ in records:
                db.update_source_item(key,item['status'],with_publication_dates(item))
            states = Counter(temporal_view(item)['status'] for _,item,_ in records)
            dates = Counter(with_publication_dates(item)['date_status'] for _,item,_ in records)
            result = {'at':utc_now(),'records':len(records),'network_attempts':len(targets),'recovered':recovered,
                      'states':dict(states),'dates':dict(dates),'fetch_errors':failures,'backup':str(backup)}
            report = directory/'publication-date-backfill.json'
            report.write_text(json.dumps(result,ensure_ascii=False,indent=2))
            print(json.dumps(result,ensure_ascii=False,indent=2))
        finally:
            db.close()


if __name__ == '__main__':
    main()
