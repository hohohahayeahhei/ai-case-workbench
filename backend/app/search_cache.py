"""Short-lived cache of successful tool citations, never model-invented URLs."""
import hashlib
import json
import time
from pathlib import Path

class SearchCache:
    def __init__(self, directory, ttl=21600):
        self.directory = Path(directory)
        self.ttl = ttl

    def path(self, query, limit):
        key = hashlib.sha256(json.dumps([query, limit], ensure_ascii=False).encode()).hexdigest()
        return self.directory / (key + '.json')

    def get(self, query, limit):
        try:
            record = json.loads(self.path(query, limit).read_text())
            age = time.time() - record['saved_at']
            if 0 <= age < self.ttl and isinstance(record['items'], list) and record['items']:
                return record['items']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def put(self, query, limit, items):
        if not items: return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            dest = self.path(query, limit)
            temp = dest.with_suffix('.tmp')
            temp.write_text(json.dumps({'saved_at':time.time(), 'items':items}, ensure_ascii=False))
            temp.replace(dest)
        except OSError:
            pass  # A cache failure must not discard successful live search results.
