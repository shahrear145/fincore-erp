#!/usr/bin/env python
"""Build `fincore-pythonanywhere.zip` — the ready-to-upload bundle.

Usage (from anywhere):

    python deploy/pythonanywhere/build_zip.py

The zip unpacks straight into ``~/fincore`` on PythonAnywhere::

    unzip fincore-pythonanywhere.zip -d ~/fincore
    bash ~/fincore/deploy/pythonanywhere/setup.sh

What goes in: the whole project except developer-only artefacts, plus a
**consistent snapshot** of the live SQLite database. The snapshot is taken with
SQLite's own backup API, so it is safe to run while the dev server is open —
copying the file directly could capture a half-written page.

Never included: `.env` (secrets), `.venv`, `staticfiles`, `backups`, caches,
`__pycache__` and the `mobile` app (it is a separate client, not part of the
Django deployment).
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "fincore-pythonanywhere.zip"
DB = ROOT / "backend" / "db.sqlite3"

TOP_FILES = ("README.md", ".gitignore")
TOP_DIRS = ("backend", "deploy", "docs")

# Any path *part* matching one of these is skipped entirely.
SKIP_DIRS = {
    ".venv", "venv", "env", "staticfiles", "backups", "media/uploads",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".git", ".idea", ".vscode",
}
SKIP_SUFFIXES = (".pyc", ".pyo", ".sqlite3-journal", ".sqlite3-wal", ".sqlite3-shm")
SKIP_NAMES = {".env", ".DS_Store", "Thumbs.db", "db.sqlite3"}


def included(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in SKIP_DIRS for part in rel.parts):
        return False
    if path.name in SKIP_NAMES:
        return False
    if path.name.endswith(SKIP_SUFFIXES):
        return False
    return True


def snapshot_db(target: Path) -> None:
    """Copy the live database with SQLite's backup API (safe while in use)."""
    if not DB.exists():
        raise SystemExit(f"database not found: {DB}")
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    check = sqlite3.connect(target)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        txns = check.execute("SELECT COUNT(*) FROM transactions_transaction").fetchone()[0]
        customers = check.execute("SELECT COUNT(*) FROM customers_customer").fetchone()[0]
        users = check.execute("SELECT COUNT(*) FROM users_user").fetchone()[0]
    finally:
        check.close()
    print(f"    db snapshot: integrity={integrity}, "
          f"{txns} transactions, {customers} customers, {users} users")
    if integrity != "ok":
        raise SystemExit("database snapshot is not consistent — aborting")


def main() -> int:
    files = []
    for name in TOP_FILES:
        path = ROOT / name
        if path.exists():
            files.append(path)
    for dirname in TOP_DIRS:
        base = ROOT / dirname
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            here = Path(dirpath)
            dirnames[:] = sorted(
                d for d in dirnames
                if included(here / d) and not (here / d).is_symlink()
            )
            for filename in sorted(filenames):
                path = here / filename
                if included(path) and path.is_file():
                    files.append(path)

    tmpdir = Path(tempfile.mkdtemp(prefix="fincore-zip-"))
    try:
        db_copy = tmpdir / "db.sqlite3"
        print("==> snapshotting the live database")
        snapshot_db(db_copy)

        if OUT.exists():
            OUT.unlink()

        print(f"==> writing {OUT.name}")
        with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(files):
                zf.write(path, path.relative_to(ROOT).as_posix())
            zf.write(db_copy, "backend/db.sqlite3")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    size_mb = OUT.stat().st_size / (1024 * 1024)
    with zipfile.ZipFile(OUT) as zf:
        count = len(zf.namelist())
    print(f"==> done: {OUT} ({count} entries, {size_mb:.2f} MB)")
    print("    upload it, then:")
    print("      unzip fincore-pythonanywhere.zip -d ~/fincore")
    print("      bash ~/fincore/deploy/pythonanywhere/setup.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
