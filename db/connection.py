"""SQLite connection: file location, PRAGMAs, row factory, schema init and migrations.

Data queries live in db/repo.py; this module only runs PRAGMAs and DDL scripts.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "expenses.db"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")
SCHEMA_VERSION = 1  # version created by schema.sql; migrations start at 002

_MIGRATION_NAME = re.compile(r"^(\d{3})_.+\.sql$")


def db_path() -> Path:
    """Database file: $EXPENSES_DB if set, else data/expenses.db."""
    return Path(os.environ.get("EXPENSES_DB") or DEFAULT_DB_PATH)


def get_conn(path: str | Path | None = None, *, init: bool = True) -> sqlite3.Connection:
    """Open the database with foreign keys on, WAL journaling and dict-like rows.

    Creates the parent folder and, unless init=False, the schema on first use.
    """
    path = Path(path) if path is not None else db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    if init:
        init_db(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def init_db(conn: sqlite3.Connection, migrations_dir: str | Path = MIGRATIONS_DIR) -> int:
    """Create the schema if needed, apply pending migrations, return the schema version.

    Safe to call repeatedly: each script runs once, tracked by PRAGMA user_version.
    """
    if schema_version(conn) < SCHEMA_VERSION:
        _apply_script(conn, SCHEMA_PATH.read_text(encoding="utf-8"), SCHEMA_VERSION)
    for version, path in _pending_migrations(Path(migrations_dir), schema_version(conn)):
        _apply_script(conn, path.read_text(encoding="utf-8"), version)
    return schema_version(conn)


def _pending_migrations(migrations_dir: Path, current: int) -> list[tuple[int, Path]]:
    seen: dict[int, Path] = {}
    for path in migrations_dir.glob("*.sql"):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise ValueError(f"migration {path.name!r} must be named NNN_description.sql")
        version = int(match.group(1))
        if version <= SCHEMA_VERSION:
            raise ValueError(f"migration {path.name!r}: numbers start at {SCHEMA_VERSION + 1:03d}")
        if version in seen:
            raise ValueError(f"migrations {seen[version].name!r} and {path.name!r} share a number")
        seen[version] = path
    return sorted((v, p) for v, p in seen.items() if v > current)


def _apply_script(conn: sqlite3.Connection, sql: str, version: int) -> None:
    """Run a DDL script and bump user_version in one transaction."""
    try:
        conn.executescript(f"BEGIN;\n{sql}\n;\nPRAGMA user_version = {int(version)};\nCOMMIT;")
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        raise
