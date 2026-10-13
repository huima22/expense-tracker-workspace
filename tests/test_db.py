"""M1: schema init, connection settings, migrations."""
import sqlite3

import pytest

from db.connection import get_conn, init_db, schema_version
from tests.conftest import dump

TABLES = {"categories", "imports", "profiles", "rules", "sessions", "transactions"}
LATEST_VERSION = 2  # schema.sql (1) + db/migrations/002_import_details.sql


def table_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {r["name"] for r in rows}


@pytest.fixture
def bare(tmp_path):
    """A connection with no schema yet, to run init_db with test migrations only."""
    connection = get_conn(tmp_path / "bare.db", init=False)
    yield connection
    connection.close()


def test_init_creates_all_tables(conn):
    assert table_names(conn) == TABLES


def test_connection_settings(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert schema_version(conn) == LATEST_VERSION
    row = conn.execute("SELECT 1 AS one").fetchone()
    assert row["one"] == 1  # sqlite3.Row factory


def test_real_migrations_applied(conn):
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(imports)")}
    assert {"headers", "unparsed"} <= columns


def test_reinit_changes_nothing(conn, db_file):
    before = dump(conn)
    assert init_db(conn) == LATEST_VERSION
    conn.close()
    reopened = get_conn(db_file)
    try:
        assert dump(reopened) == before
        assert schema_version(reopened) == LATEST_VERSION
    finally:
        reopened.close()


def test_env_var_overrides_db_path(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "dir" / "custom.db"
    monkeypatch.setenv("EXPENSES_DB", str(target))
    conn = get_conn()
    try:
        assert target.exists()
        assert table_names(conn) == TABLES
    finally:
        conn.close()


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO imports (session_id, profile_id, filename, file_sha256, row_count)"
                " VALUES (999, 999, 'x.csv', 'abc', 0)"
            )


def test_migrations_apply_once_in_order(bare, tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "003_more.sql").write_text("ALTER TABLE notes ADD COLUMN body TEXT;")
    (migrations / "002_notes.sql").write_text("CREATE TABLE notes (id INTEGER PRIMARY KEY);")

    assert init_db(bare, migrations) == 3
    assert "notes" in table_names(bare)
    assert init_db(bare, migrations) == 3  # no IF NOT EXISTS: a re-run would fail


def test_failed_migration_rolls_back(bare, tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "002_bad.sql").write_text("CREATE TABLE half_done (id INTEGER); SELECT * FROM missing;")

    with pytest.raises(sqlite3.OperationalError):
        init_db(bare, migrations)
    assert schema_version(bare) == 1
    assert "half_done" not in table_names(bare)


@pytest.mark.parametrize("filename", ["2_short.sql", "notes.sql", "001_too_low.sql"])
def test_badly_named_migration_is_rejected(bare, tmp_path, filename):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / filename).write_text("SELECT 1;")
    with pytest.raises(ValueError):
        init_db(bare, migrations)
