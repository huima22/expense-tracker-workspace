"""Create data/ and the database schema, then apply pending migrations. Safe to re-run."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python scripts/init_db.py`

from db.connection import db_path, get_conn, init_db  # noqa: E402


def main() -> None:
    conn = get_conn(init=False)
    try:
        version = init_db(conn)
    finally:
        conn.close()
    print(f"Database ready at {db_path()} (schema version {version})")


if __name__ == "__main__":
    main()
