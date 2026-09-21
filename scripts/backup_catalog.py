"""Back up only this project's case catalog and verify SQLite integrity."""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.config import runtime_dir


def backup() -> Path:
    directory = runtime_dir()
    source_path = directory / "catalog.db"
    if not source_path.exists():
        raise RuntimeError("No catalog yet; run a collection first")
    backup_dir = directory / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / ("catalog-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".db")
    with sqlite3.connect(f"file:{source_path}?mode=ro", uri=True) as source, sqlite3.connect(path) as destination:
        source.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup failed integrity check")
    return path


if __name__ == "__main__":
    print(json.dumps({"status": "verified", "backup": str(backup())}, ensure_ascii=False))
