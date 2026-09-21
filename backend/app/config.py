"""Small, dependency-free loader for the project's local .env file."""

from __future__ import annotations

import os
import re
from pathlib import Path


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_local_env(path: str | Path | None = None) -> None:
    """Load simple KEY=VALUE entries without overriding process variables."""
    env_path = Path(path) if path else Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not _KEY.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def runtime_dir() -> Path:
    """Durable personal knowledge base; tests override this with a temp folder."""
    load_local_env()
    path = Path(os.environ.get("AI_CASE_DATA_DIR", str(Path(__file__).resolve().parents[2] / "data/runtime")))
    path.mkdir(parents=True, exist_ok=True)
    return path
