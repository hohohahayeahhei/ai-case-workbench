"""Repeatable engineering loop for the local portfolio project."""

from __future__ import annotations

import subprocess
import sys
import ast
import os
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/bin/python"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)


def run(label: str, command: list[str], cwd: Path = ROOT) -> None:
    print(f"\n[CHECK] {label}")
    result = subprocess.run(command, cwd=cwd, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)


def parse_python_sources() -> None:
    """Parse sources without writing .pyc files in restricted workspaces."""
    print("\n[CHECK] Python syntax")
    for path in (ROOT / "backend/app").glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ai-case-self-check-") as directory:
        os.environ.update(AI_CASE_DATA_DIR=directory, AI_CASE_LLM_MODE="mock", AI_CASE_LLM_WIRE_API="chat",
                          AI_CASE_NOTIFICATIONS_ENABLED="false", PYTHONDONTWRITEBYTECODE="1")
        checks()


def checks() -> None:
    parse_python_sources()
    run("Python regression tests", [str(PYTHON), "-m", "unittest", "discover", "-s", "backend/tests", "-v"])
    run("Official MCP stdio protocol", [str(PYTHON), "scripts/check_mcp_protocol.py"])
    run("Offline collection smoke test", [str(PYTHON), "scripts/run_collection.py", "--source-mode", "online_snapshot"])
    run("Frontend production build", ["npm", "run", "build", "--", "--outDir", "/tmp/ai-case-workbench-self-check-dist"], ROOT / "frontend")
    print("\n[PASS] self-check completed")


if __name__ == "__main__":
    main()
