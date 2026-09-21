"""Keep persisted catalog status and score policy aligned, without network calls."""
import json
from .scoring import score_case, SCORING_VERSION
from .db import utc_now


def reconcile_scores(db):
    changed = 0
    rows = db.connection.execute("SELECT source_item_id,status,payload_json FROM source_items WHERE status IN ('selected','featured','candidate','needs_scoring','rejected')").fetchall()
    with db.connection:
        for row in rows:
            item = json.loads(row['payload_json'])
            if not all(item.get(k,{}).get('evidence') for k in ('problem','approach','outcome')):
                continue
            if row['status'] == 'rejected':
                item['status'] = 'rejected'
            card = score_case(item)
            status = 'selected' if card['selected'] else card['tier']
            if item.get('scorecard') == card and row['status'] == status and item.get('status') == status:
                continue
            old = item.get('scorecard')
            if old and old.get('score_version') != SCORING_VERSION:
                item.setdefault('score_history',[]).append({'at':utc_now(),'status':row['status'],'scorecard':old})
                item['attempt_count'] = 0
            item.update(scorecard=card,status=status)
            db.connection.execute('UPDATE source_items SET status=?,payload_json=? WHERE source_item_id=?',
                                  (status,json.dumps(item,ensure_ascii=False),row['source_item_id']))
            changed += 1
    return changed
