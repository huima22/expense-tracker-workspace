# Local Expense Tracker

A single-user Streamlit app that reads bank exports (CSV/XLSX), applies your rules to categorise or skip each row, and totals expenses per category. All data stays on your machine in one SQLite file (`data/expenses.db`).

See `DESIGN.md` for the full design.

## Prerequisites

- Python 3.11 or newer (`python --version`)
- Windows PowerShell (commands below; macOS/Linux differences are noted)

## First-time setup

Run these from the project root.

1. Create a virtual environment:

   ```powershell
   python -m venv .venv
   ```

2. Activate it:

   ```powershell
   .venv\Scripts\activate
   ```

   On macOS/Linux use `source .venv/bin/activate`. If PowerShell says running scripts is disabled, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then try again.

3. Install the dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

4. Create the database (creates `data/expenses.db` and applies migrations):

   ```powershell
   python scripts/init_db.py
   ```

5. Load the starter categories, bank profiles and rules from `config/seed.yaml`:

   ```powershell
   python scripts/seed.py
   ```

6. Check that everything works:

   ```powershell
   pytest
   ```

Steps 4 and 5 are safe to re-run; they never delete your data.

## Starting the app

```powershell
.venv\Scripts\activate
streamlit run app.py
```

Streamlit opens the app in your browser at <http://localhost:8501>. Stop it with `Ctrl+C` in the terminal.

## Trying it out

The repo includes fake bank files in `tests/fixtures/` (`dbs_sample.csv`, `citi_sample.xlsx`, and others). Upload one in the app to see the flow: upload → preview → rules → category result → consolidated totals.

## Useful extras

- **Use a different database file:** set `EXPENSES_DB` before starting, e.g. `$env:EXPENSES_DB = "data\test.db"`.
- **Start over with an empty database:** stop the app, delete `data\expenses.db` (plus any `-wal`/`-shm` files beside it), then re-run steps 4 and 5.
- **Change the starter config:** edit `config/seed.yaml` and re-run `python scripts/seed.py`; entries are updated in place.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `streamlit` or `pytest` is not recognised | The virtual environment isn't active; run `.venv\Scripts\activate`. |
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` with the virtual environment active. |
| Port 8501 is already in use | `streamlit run app.py --server.port 8502` |
| `database is locked` | Close other programs that have `expenses.db` open (e.g. DB Browser for SQLite). |

Never commit real bank files; `.gitignore` excludes `*.csv`, `*.xlsx` and the database, except the fake fixtures.
