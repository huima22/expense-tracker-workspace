# CLAUDE.md

Build conventions for the Local Expense Tracker.

- Follow `DESIGN.md`; build one milestone at a time in the order of section 10.
- Money is always integer cents (`amount_cents`, `signed_amount_cents`); never floats. Parse amounts via `Decimal`.
- `core/` never imports Streamlit; UI code only calls `core/` and `db/repo.py`.
- `core/pipeline.py` is the only core module that touches the database (via `db/repo.py`); parsers, profiles, rules and summary stay pure.
- After any rule or decision change, re-run rules via `pipeline.rerun_session()`; user decisions (`decided_by='user'`) survive re-runs.
- UI tests use `streamlit.testing.v1.AppTest` (see `tests/test_app.py`); AppTest can't upload files, so import fixtures via the pipeline first.
- All data SQL lives in `db/repo.py` and uses parameterised queries; `db/connection.py` only runs PRAGMAs, `schema.sql` and migrations.
- Config is validated by the pydantic models in `core/models.py` before it reaches SQLite; repo write functions re-validate.
- Every SQLite connection sets `PRAGMA foreign_keys = ON`, `PRAGMA journal_mode = WAL`, and `sqlite3.Row` as row factory.
- Run `pytest` after each milestone; a milestone is done only when its acceptance criteria pass.
- Never commit real bank files; use the fake fixtures in `tests/fixtures/`.

## Commands (Windows, from the project root)

```powershell
.venv\Scripts\activate
pip install -r requirements.txt
python scripts/init_db.py
python scripts/seed.py
pytest
streamlit run app.py
```
