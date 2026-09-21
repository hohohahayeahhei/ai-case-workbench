#!/usr/bin/env python3
"""Deploy this personal app outside macOS protected Desktop folders.

Run after stopping this project's API and UI. No process is killed by this
script. Existing data and LaunchAgent definitions are retained as backups.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABELS = ("com.ai-case-workbench", "com.ai-case-workbench-daily", "com.ai-case-workbench-health")
DEFAULT_DESTINATION = Path.home() / "Library/Application Support/ai-case-workbench"
SEARCH_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, text=True, capture_output=True)


def check_database_unused(runtime: Path) -> None:
    """Do not split a catalog while a manually started API holds it open."""
    database = runtime / "catalog.db"
    if not database.exists():
        raise RuntimeError("The source catalog.db is missing; refusing to deploy an empty replacement.")
    result = run("/usr/sbin/lsof", "-t", "--", str(database), check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError("Cannot verify whether catalog.db is open; stop the project services and retry.")
    if result.stdout.strip():
        pids = ", ".join(sorted(set(result.stdout.split())))
        raise RuntimeError(f"catalog.db is still open by process(es) {pids}; stop those project services first.")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
        if reader.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Source catalog.db failed its integrity check.")
        active = reader.execute("SELECT COUNT(*) FROM collection_runs WHERE status IN ('running','queued')").fetchone()[0]
        if active:
            raise RuntimeError("The catalog has running or queued collections; finish or recover them before deployment.")


@contextmanager
def collection_lock(runtime: Path):
    with (runtime / "catalog.db.lock").open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("A collection is active; wait for it to finish before deployment.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def sqlite_backup(source: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.name + ".deploy-backup")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader, closing(sqlite3.connect(temporary)) as writer:
        reader.backup(writer)
        if writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {source.name}")
    temporary.replace(destination)


def migrate_runtime(destination: Path, backup_root: Path) -> dict:
    source = ROOT / "data/runtime"
    target = destination / "data/runtime"
    if source.is_symlink():
        if source.resolve() != target.resolve() or not target.is_dir():
            raise RuntimeError("data/runtime already points somewhere else; refusing to replace it.")
        return {"migrated": False, "canonical_runtime": str(target)}
    if not source.is_dir():
        raise RuntimeError("Project data/runtime does not exist.")
    if target.exists():
        raise RuntimeError("Destination runtime already exists without the project symlink; refusing to merge databases.")
    with collection_lock(source):
        check_database_unused(source)
        if any(item.is_symlink() for item in source.rglob("*")):
            raise RuntimeError("Unexpected symlink in runtime; refusing an ambiguous migration.")
        staging = target.with_name("runtime-staging-" + stamp())
        staging.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, staging, ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm"))
        for database in source.rglob("*.db"):
            if database.is_symlink():
                raise RuntimeError("Unexpected database symlink in runtime; refusing an ambiguous migration.")
            with database.open("rb") as handle:
                is_sqlite = handle.read(16) == b"SQLite format 3\x00"
            if is_sqlite:
                sqlite_backup(database, staging / database.relative_to(source))
        # Backups are retained permanently; there is no delete/replace of old data.
        original = backup_root / ("runtime-before-background-" + stamp())
        backup_root.mkdir(parents=True, exist_ok=True)
        staging.rename(target)
        source.rename(original)
        try:
            source.symlink_to(target, target_is_directory=True)
        except OSError:
            original.rename(source)
            raise
        return {"migrated": True, "canonical_runtime": str(target), "original_runtime_backup": str(original)}


def copy_code(destination: Path) -> None:
    def refresh_tree(source: Path, target: Path) -> None:
        if target.is_symlink():
            raise RuntimeError(f"A deployment directory unexpectedly became a symlink: {target.name}")
        target.mkdir(parents=True, exist_ok=True)
        for entry in source.iterdir():
            if entry.name in {"__pycache__", ".DS_Store", ".pytest_cache"} or entry.suffix == ".pyc":
                continue
            out = target / entry.name
            if entry.is_symlink():
                link = os.readlink(entry)
                if out.is_symlink() and os.readlink(out) == link:
                    continue
                temporary = out.with_name(out.name + ".deploy-link-" + stamp())
                temporary.symlink_to(link)
                temporary.replace(out)
            elif entry.is_dir():
                refresh_tree(entry, out)
            else:
                if out.is_symlink():
                    raise RuntimeError(f"A deployment file unexpectedly became a symlink: {out.name}")
                shutil.copy2(entry, out)

    for directory in ("backend", "scripts", "skills", "docs", "deploy", "data/fixtures", "frontend", ".venv"):
        source = ROOT / directory
        if not source.is_dir():
            raise RuntimeError(f"Required project directory is missing: {directory}")
        refresh_tree(source, destination / directory)
    for filename in ("pyproject.toml", "README.md", ".env.example"):
        source = ROOT / filename
        if source.exists():
            shutil.copy2(source, destination / filename)


def set_runtime_env(destination: Path, backup_root: Path) -> None:
    source = ROOT / ".env"
    if not source.is_file():
        raise RuntimeError("Project .env is missing; configure the project before deploying.")
    previous = source.read_text(encoding="utf-8")
    entries = previous.splitlines()
    updated = []
    for line in entries:
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if stripped.split("=", 1)[0].strip() != "AI_CASE_DATA_DIR":
            updated.append(line)
    updated.append("AI_CASE_DATA_DIR=" + str(destination / "data/runtime"))
    contents = "\n".join(updated) + "\n"
    if contents != previous:
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / ("env-before-background-" + stamp())
        shutil.copy2(source, backup)
        backup.chmod(0o600)
        write_private(source, contents)
    else:
        source.chmod(0o600)
    target = destination / ".env"
    write_private(target, contents)


def write_private(path: Path, contents: str) -> None:
    temporary = path.with_name(path.name + ".deploy-" + stamp())
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(contents)
    temporary.replace(path)


def install_jobs(destination: Path, backup_root: Path) -> None:
    launch_dir = Path.home() / "Library/LaunchAgents"
    launch_dir.mkdir(parents=True, exist_ok=True)
    (destination / "data/runtime/logs").mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    staged = []
    # Build and validate every definition before replacing any installed job.
    for label in LABELS:
        text = (destination / "deploy" / f"{label}.plist").read_text(encoding="utf-8")
        # Replace after parsing, so paths containing XML-special characters are safe.
        def substitute(value):
            if isinstance(value, str):
                return value.replace("__PROJECT_ROOT__", str(destination)).replace("__USER_HOME__", str(Path.home()))
            if isinstance(value, list):
                return [substitute(item) for item in value]
            if isinstance(value, dict):
                return {key: substitute(item) for key, item in value.items()}
            return value
        payload = substitute(plistlib.loads(text.encode()))
        payload.setdefault("EnvironmentVariables", {})["PATH"] = SEARCH_PATH
        staged.append((label, plistlib.dumps(payload)))
    for label, contents in staged:
        target = launch_dir / f"{label}.plist"
        if target.exists():
            saved = backup_root / ("launch-agents-before-background-" + stamp())
            saved.mkdir(parents=True)
            shutil.copy2(target, saved / target.name)
        run("/bin/launchctl", "bootout", f"gui/{uid}/{label}", check=False)
        target.write_bytes(contents)
        target.chmod(0o644)
        run("/bin/launchctl", "enable", f"gui/{uid}/{label}")
        run("/bin/launchctl", "bootstrap", f"gui/{uid}", str(target))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--install", action="store_true", help="Copy code, migrate data, and install all three LaunchAgents")
    parser.add_argument("--prepare-only", action="store_true", help="Copy code only; no data, configuration, or LaunchAgent changes")
    args = parser.parse_args()
    if args.install == args.prepare_only:
        parser.error("Choose exactly one of --install and --prepare-only")
    destination = args.destination.expanduser().resolve()
    if destination == ROOT or ROOT in destination.parents or destination in ROOT.parents:
        parser.error("Destination must be a separate application directory outside the project")
    protected = (Path.home() / "Desktop", Path.home() / "Documents", Path.home() / "Downloads")
    if any(path == destination or path in destination.parents for path in protected):
        parser.error("Use an application directory outside Desktop, Documents, and Downloads")
    if not (ROOT / ".env").is_file():
        raise RuntimeError("Project .env is missing; configure the project before deploying.")
    if args.install:
        source_runtime = ROOT / "data/runtime"
        with collection_lock(source_runtime):
            check_database_unused(source_runtime)
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o700)
    copy_code(destination)
    python = destination / ".venv/bin/python"
    # Import only: no network, database writes, or collection is triggered.
    run(str(python), "-c", "import fastapi, uvicorn, langgraph, pydantic; print('runtime dependencies available')")
    if args.prepare_only:
        print(f"Code prepared at {destination}; data and LaunchAgents unchanged.")
        return
    backup_root = ROOT / "data/backups"
    details = migrate_runtime(destination, backup_root)
    set_runtime_env(destination, backup_root)
    install_jobs(destination, backup_root)
    print(f"Installed application at {destination}")
    print(f"Canonical data: {details['canonical_runtime']}")
    if details.get("original_runtime_backup"):
        print(f"Original data retained at {details['original_runtime_backup']}")
    print("Daily collection is scheduled for 08:30 local time; it was not started by this installer.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError, sqlite3.Error) as exc:
        # Never echo commands' captured output; environment/provider errors may
        # include credentials. Detailed launch status is available via launchctl.
        if isinstance(exc, subprocess.CalledProcessError):
            print(f"Deployment failed: subprocess exited with code {exc.returncode}.", file=sys.stderr)
        else:
            print(f"Deployment failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
