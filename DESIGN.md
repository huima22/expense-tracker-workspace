# Local Expense Tracker — Design Document

Oct 10, 2026 · @Ma Hui

## 1. Overview

The app is a single-user, local Streamlit tool. It reads bank transaction exports (CSV and XLSX), applies user-defined rules to skip or categorise each row, and outputs total expense per category plus a grand total across all uploaded files.

**How to use this document with Claude Code:** put it in the project root as `DESIGN.md` (export this doc as Markdown). Then ask Claude Code to build it milestone by milestone, using section 10 as the build order. Each milestone has acceptance criteria to check before moving on.

**In scope**

- Upload one or more bank files in a session; each file goes through upload → preview → rules → category result.
- Bank profiles: one profile per bank file format (column mapping, header row, date format) plus sign rules that decide whether each row is expense or income.
- Predefined categories, each owning one or more rules; skip rules for refunds and own-account transfers.
- A review step for rows no rule matches, with "assign once" or "assign + create rule".
- Consolidation across all files in the session: expense per category and total, exportable to CSV/XLSX.
- Persistence in a local SQLite file, so profiles, categories and rules carry over between runs.

**Out of scope for v1**

- PDF statements. The design leaves a slot for them (section 11).
- Multiple currencies. All amounts are treated as one currency (SGD).
- Hosting, logins, multiple users, bank API connections.

## 2. Tech stack

Everything is Python 3.11+, runs on localhost, and stores data in one SQLite file.

| Layer | Choice | Why |
| --- | --- | --- |
| UI | Streamlit ≥ 1.38 | File uploader, `st.data_editor` for editable tables, `st.session_state` for the multi-file session; `streamlit run app.py` opens it in the browser |
| Parsing | pandas + openpyxl | Reads CSV and XLSX into DataFrames; openpyxl is the XLSX engine |
| Storage | SQLite via Python's built-in `sqlite3` | No server, no install; the whole app state is one `data/expenses.db` file |
| Models | pydantic v2 | Validates profiles and rules so a bad config fails loudly |
| Config | PyYAML | Seed file for starter categories, rules and profiles |
| Tests | pytest | Unit tests for parsers, normaliser and rule engine |
| Later | pdfplumber | PDF statement tables (not installed in v1) |

No ORM: the schema is small, and plain SQL in one repository module keeps it transparent. FastAPI + a JS front end was rejected as two processes and a build step for no benefit in a single-user local app.

## 3. Environment and SQLite setup

SQLite needs no server: Python ships the `sqlite3` driver, and the database is created on first run. The steps below give you a working environment plus tools to inspect the data.

**Step 1. Python and project folder**

```bash
python3 --version            # needs 3.11 or newer
mkdir expense_tracker && cd expense_tracker
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
```

**Step 2. Dependencies** — create `requirements.txt`:

```text
streamlit>=1.38
pandas>=2.2
openpyxl>=3.1
pydantic>=2.7
pyyaml>=6.0
pytest>=8.0
```

```bash
pip install -r requirements.txt
python -c "import sqlite3; print(sqlite3.sqlite_version)"   # expect 3.35+
```

**Step 3. Optional tools for looking at the database**

- SQLite CLI: macOS has it already (`sqlite3 --version`); Windows: `winget install SQLite.SQLite`; Ubuntu: `sudo apt install sqlite3`.
- GUI: DB Browser for SQLite (`brew install --cask db-browser-for-sqlite`), or the VS Code extension "SQLite Viewer".

**Step 4. Database location and connection settings**

- File: `data/expenses.db`, overridable with env var `EXPENSES_DB`. Add `data/*.db` to `.gitignore`.
- Every connection runs `PRAGMA foreign_keys = ON;` (SQLite leaves it off by default) and `PRAGMA journal_mode = WAL;`.
- Use `sqlite3.Row` as the row factory so rows read like dicts.
- Store money as integer cents (`amount_cents`), never floats, so totals don't drift.

**Step 5. Initialise the schema** — `db/schema.sql` holds the DDL from section 5. `scripts/init_db.py` must:

1. Create `data/` if missing.
2. Run `schema.sql` (all statements use `IF NOT EXISTS`, so re-running is safe).
3. Set `PRAGMA user_version = 1`. Later schema changes go in `db/migrations/002_*.sql` etc., applied in order when `user_version` is lower.

```bash
python scripts/init_db.py
```

The app also calls the same init function at start-up, so this step is only needed for a manual reset.

**Step 6. Seed starter data** — `scripts/seed.py` loads `config/seed.yaml` (categories, skip rules, category rules, and the DBS profile) with upserts, so it can be re-run safely.

```bash
python scripts/seed.py
```

**Step 7. Verify**

```bash
sqlite3 data/expenses.db ".tables"
# categories  imports  profiles  rules  sessions  transactions
sqlite3 data/expenses.db "SELECT name FROM categories ORDER BY sort_order;"
sqlite3 data/expenses.db "PRAGMA foreign_keys; PRAGMA user_version;"
```

**Step 8. Run the app**

```bash
streamlit run app.py         # opens http://localhost:8501
```

**Backup and reset:** copy `data/expenses.db` to back up everything. Delete it and re-run steps 5–6 to start fresh.

## 4. Architecture and pipeline

Each file runs through the same five pure stages, DataFrame in and DataFrame out; only stage 1 knows the file format, and nothing after stage 3 knows which bank the row came from.

1. **Parse** — the parser registry picks a parser by extension (`.csv`, `.xlsx`; `.pdf` later) and returns a raw DataFrame with every cell as a string and columns named by Excel letter (A, B, C…).
2. **Detect profile** — match the filename against each profile's `filename_pattern`, then confirm the expected header cells sit in `header_row`. No match opens the Header & columns step (section 6), where the user sets the header row and column mapping and saves it as a profile.
3. **Normalise** — apply the profile: drop preamble and footer rows, map columns to `date`, `description`, `amount`, parse dates, run the profile's sign rules to mark each row expense or income. Output is the canonical transaction (section 6).
4. **Dedupe** — hash `profile_id + date + amount_cents + description + occurrence index`; rows whose hash already exists in a *committed* import created earlier (any session) are marked duplicate and excluded. Duplicates are re-checked on every re-run, so removing the original import brings its copies back.
5. **Apply rules** — sign rules first, then review, skip and category rules by priority, first match wins (section 7). Refunds (review rules) and unmatched expenses are `uncategorised`.

Then per file the UI shows the category result; on **Consolidate**, the summary module aggregates all files in the current session from SQLite.

**Session model.** A *session* is one import run (e.g. "October"). Files are added to the session one at a time; rules can change while working, and every rule change re-runs stage 5 for all files in the session, so results always reflect the current rules. Manual assignments survive re-runs.

## 5. Data model (`db/schema.sql`)

Six tables: configuration (`profiles`, `categories`, `rules`) and data (`sessions`, `imports`, `transactions`). Money is integer cents; dates are ISO `YYYY-MM-DD` text.

```sql
CREATE TABLE IF NOT EXISTS profiles (
  id               INTEGER PRIMARY KEY,
  name             TEXT NOT NULL UNIQUE,          -- 'DBS Savings CSV'
  file_type        TEXT NOT NULL CHECK (file_type IN ('csv','xlsx','pdf')),
  filename_pattern TEXT,                          -- regex, e.g. '(?i)^dbs.*\.csv$'
  header_row       INTEGER NOT NULL DEFAULT 1,    -- 1-based row holding column headers
  header_signature TEXT,                          -- JSON: {"A":"Transaction Date","L":"Description"}
  skip_footer_rows INTEGER NOT NULL DEFAULT 0,
  sheet_name       TEXT,                          -- xlsx only; NULL = first sheet
  column_map       TEXT NOT NULL,                 -- JSON, see section 6
  date_format      TEXT NOT NULL,                 -- strptime, e.g. '%d %b %Y'
  encoding         TEXT NOT NULL DEFAULT 'utf-8-sig',
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categories (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  color      TEXT NOT NULL DEFAULT '#9AA3AE',
  sort_order INTEGER NOT NULL DEFAULT 0,
  is_active  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS rules (
  id           INTEGER PRIMARY KEY,
  action       TEXT NOT NULL CHECK (action IN ('sign','review','skip','categorise')),
  sign_result  TEXT CHECK (sign_result IN ('expense','income')),   -- sign rules only
  category_id  INTEGER REFERENCES categories(id) ON DELETE CASCADE, -- categorise only
  target_field TEXT NOT NULL DEFAULT 'description'
               CHECK (target_field IN ('description','amount','column')),
  column_ref   TEXT,                              -- target_field = 'column': letter or header, e.g. 'M'
  operator     TEXT NOT NULL CHECK (operator IN
               ('contains','starts_with','equals','regex','amount_between',
                'is_negative','is_positive','not_empty')),
  pattern      TEXT,                              -- text/regex; amount_between: '10,50'
  profile_id   INTEGER REFERENCES profiles(id) ON DELETE CASCADE,  -- NULL = all profiles
  priority     INTEGER NOT NULL DEFAULT 100,      -- lower runs first, within its action
  enabled      INTEGER NOT NULL DEFAULT 1,
  note         TEXT,                              -- shown as the reason, e.g. 'Possible refund'
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK ((action = 'categorise') = (category_id IS NOT NULL)),
  CHECK ((action = 'sign')       = (sign_result IS NOT NULL)),
  CHECK (action <> 'sign' OR profile_id IS NOT NULL)   -- sign rules are per bank format
);

CREATE TABLE IF NOT EXISTS sessions (
  id              INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,                  -- 'October 2026'
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  consolidated_at TEXT
);

CREATE TABLE IF NOT EXISTS imports (
  id          INTEGER PRIMARY KEY,
  session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  profile_id  INTEGER NOT NULL REFERENCES profiles(id),
  filename    TEXT NOT NULL,
  file_sha256 TEXT NOT NULL,
  row_count   INTEGER NOT NULL,
  status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','committed')),
  imported_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
  id                  INTEGER PRIMARY KEY,
  import_id           INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
  row_hash            TEXT NOT NULL,
  txn_date            TEXT NOT NULL,
  description         TEXT NOT NULL,
  signed_amount_cents INTEGER NOT NULL,           -- exactly as the file shows it
  amount_cents        INTEGER NOT NULL,           -- ABS(signed_amount_cents)
  is_debit            INTEGER,                    -- 1 expense, 0 income, NULL = no sign rule matched
  status              TEXT NOT NULL CHECK (status IN
                      ('categorised','skipped','uncategorised','needs_sign','duplicate')),
  review_reason       TEXT,                       -- set by a review rule, e.g. 'Possible refund'
  decided_by          TEXT NOT NULL DEFAULT 'rule' CHECK (decided_by IN ('rule','user','default')),
  category_id         INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  matched_rule_id     INTEGER REFERENCES rules(id) ON DELETE SET NULL,
  raw_row             TEXT NOT NULL               -- JSON of the original cells
);

CREATE INDEX IF NOT EXISTS ix_txn_import   ON transactions(import_id);
CREATE INDEX IF NOT EXISTS ix_txn_hash     ON transactions(row_hash);
CREATE INDEX IF NOT EXISTS ix_txn_category ON transactions(category_id);
CREATE INDEX IF NOT EXISTS ix_rules_order  ON rules(enabled, action, priority);
```

**Consolidated summary query** (what the Consolidate button runs):

```sql
-- Expenses add, kept refunds (income the user chose to Keep) subtract from their category.
-- Pending items (needs_sign, or uncategorised income awaiting Keep/Skip) are left out
-- and shown as a warning instead.
SELECT COALESCE(c.name, 'Uncategorised') AS category,
       SUM(CASE WHEN t.is_debit = 1 THEN t.amount_cents
                ELSE -t.amount_cents END) / 100.0 AS total_expense
FROM transactions t
JOIN imports i         ON i.id = t.import_id
LEFT JOIN categories c ON c.id = t.category_id
WHERE i.session_id = :session_id
  AND (   t.status = 'categorised'
       OR (t.status = 'uncategorised' AND t.is_debit = 1))
GROUP BY category
ORDER BY total_expense DESC;
```

The grand total is the sum of that result; the UI computes it rather than running a second query.

## 6. Profiles and normalisation

A profile turns one bank's raw layout into the canonical transaction; one profile per bank file format.

**Raw reading.** Parsers return the file with no header: every row as strings (empty cells are `""`), padded to the widest row's length, columns named by Excel letter (A, B … Z, AA …) and rows numbered from 1. CSV is read with Python's `csv` module, not `pd.read_csv`, because bank preambles (account number, statement period…) have fewer fields than the data rows; encodings tried in order: the profile's, `utf-8-sig`, `cp1252`. XLSX is read with openpyxl (values only) from the profile's sheet or the first sheet; date cells become ISO text. Fully empty rows at the end of the file are dropped. `raw_preview(file_bytes, file_type, n=25)` returns the first rows for the UI.

**`column_map` JSON.** Values are Excel column letters (`"L"`); profiles saved from the UI always store letters. Exact header names (`"Description"`) are still accepted in hand-written profiles and resolved against the header row. Letters map to a 0-based index (A=0, L=11, Z=25, AA=26).

```json
{
  "date": "A",
  "description": "L",
  "amount": null,
  "debit": "M",
  "credit": "N",
  "extra_description": ["C", "D"]
}
```

- The normaliser never decides direction. It stores `signed_amount_cents` exactly as the file shows it (from `amount`, or from whichever of `debit` / `credit` holds a value) and `amount_cents` as its absolute value.
- Expense vs income is decided by the profile's **sign rules** (section 7), because banks disagree: some show spend as negative, some as positive, some use separate debit and credit columns, some a DR/CR indicator column.
- `extra_description` (optional): extra columns appended to the description with a space, for banks that split merchant text.

**Canonical transaction** (pydantic model `Txn`):

| Field | Type | Notes |
| --- | --- | --- |
| `txn_date` | date | Parsed with the profile's `date_format` |
| `description` | str | Whitespace collapsed, original case kept for display |
| `signed_amount_cents` | int | As in the file, e.g. -18.40 → -1840 |
| `amount_cents` | int | Absolute value, always positive |
| `is_debit` | bool or None | Set by sign rules: True = expense, False = income, None = no sign rule matched |
| `row_hash` | str | sha256 of profile id, date, signed cents, lower-cased description, occurrence index |
| `raw_row` | dict | Original cells keyed by column letter |

**Normalisation rules**

1. Read every cell as a string (see Raw reading) so the parser never guesses types.
2. Drop rows above and including `header_row` and the last `skip_footer_rows` rows; drop rows where date and amount are both empty.
3. Amounts: strip currency symbols, spaces and thousands commas; `(12.00)` means −12.00; convert via `Decimal` to cents.
4. Rows that fail date or amount parsing are not dropped silently: they appear in the preview flagged "unparsed" and are excluded from totals.
5. The occurrence index in the hash keeps two genuine identical purchases on the same day (two coffees) from collapsing into one.

**Seed profile — DBS Savings CSV** (adjust after checking a real export):

```yaml
name: DBS Savings CSV
file_type: csv
filename_pattern: '(?i)dbs.*\.csv$'
header_row: 6
column_map: {date: A, description: L, debit: M, credit: N}
date_format: '%d %b %Y'
sign_rules:
  - {priority: 1, target_field: column, column_ref: M, operator: not_empty, sign_result: expense}
  - {priority: 2, target_field: column, column_ref: N, operator: not_empty, sign_result: income}
```

**Detection.** `detect_profile()` keeps profiles of the same file type whose `filename_pattern` matches the file name, then confirms that every `header_signature` cell (letter → header text, compared case-insensitively) sits in that profile's `header_row`, read with the profile's sheet and encoding. The first full match is used and the file imports straight away. No match returns `None` and the UI opens the Header & columns step. A profile that matches by filename but fails the signature check (the bank changed its layout, or the seed profile doesn't fit a real export) is also treated as no match, but the step opens pre-filled with that profile's settings and saving updates it. A file renamed so that no pattern matches is not auto-detected.

**Header & columns step** (pure logic in `core/mapping.py`, UI in `ui/profile_form.py`). Header rows can't be found reliably by guessing, so the user sets them:

1. *Raw view* — the first 25 raw rows with row numbers and column letters; the chosen header row is highlighted, skipped rows greyed out. An expander shows the last rows with footer rows struck through.
2. *Inputs* — "Rows to skip before the header" (header row = skip + 1), pre-filled with a suggestion shown in the label ("suggested: N": the first row with 3+ filled cells containing a header word such as date, description or amount; failing that, the row above the first date-like row) and always editable; "Rows to skip at the end (footer)"; "Sheet" for XLSX.
3. *Mapping* — columns listed as `L · header text`: date column and date format (`%d %b %Y`, `%d/%m/%Y`, `%Y-%m-%d`, `%d-%m-%Y`, `%m/%d/%Y` or custom), description column, optional extra description columns, and the amount layout — one of the four rows of the sign-rule table in section 7 (spending positive, spending negative, separate debit/credit, amount + DR/CR indicator with editable indicator values). Column, date-format and layout suggestions come from header words and sample dates.
4. *Live check* — re-normalises on every change and shows the first 10 rows (date, description, signed amount, expense/income) and "Dates parsed: 41/42 · Amounts parsed: 42/42 · Expense rows: 38 · Income rows: 4"; failing rows are listed in an expander. Save stays disabled until date, description and amount are mapped and at least 90% of data rows parse.
5. *Save as profile* — name (default: bank guessed from the file name's leading word + file type, e.g. "DBS CSV") and an editable `filename_pattern` (default `(?i)^<leading word>.*\.<ext>$`). Saving writes `header_row`, `skip_footer_rows`, `sheet_name`, `column_map` (letters), `date_format`, `encoding` and `header_signature` (letter → header text of the mapped columns), replaces the profile's sign rules with the two for the chosen layout (one transaction, `repo.save_profile_with_sign_rules`), imports the file and re-runs the session's rules.

"Edit mapping" on a file card reopens the step pre-filled from the file's profile; saving updates the profile and re-imports the file (manual decisions on that file's rows are cleared).

## 7. Rule engine

Every transaction first gets a direction (expense or income) from sign rules, then exactly one outcome: categorised, skipped, uncategorised or needs\_sign. It records the rule that decided it.

**Evaluation order**

1. If `decided_by = 'user'`, keep it as is.
2. **Sign rules** for this profile, ascending priority. First match sets `is_debit` (expense = 1, income = 0). No match → `needs_sign`: left out of totals until the user picks Expense or Income, with an option to save that as a new sign rule.
3. **Review rules**, ascending priority. First match → `uncategorised` with `review_reason` from the rule's note, e.g. "Possible refund". The user chooses **Keep** (pick a category; a kept income row reduces that category's total) or **Skip**.
4. **Skip rules** → `skipped`.
5. Income rows not caught by a review rule → `skipped` with `decided_by = 'default'`, so salary, interest and cashback never count.
6. **Category rules** on expense rows, ascending priority, ties broken by rule id. First match → `categorised`.
7. No match → `uncategorised`.

**Operators** (text comparisons are case-insensitive and run on whitespace-collapsed text):

| Operator | Applies to | Pattern example | Matches |
| --- | --- | --- | --- |
| `contains` | description or column | `grab` | "GRAB\*TRANSPORT SG" |
| `starts_with` | description or column | `ntuc` | "NTUC FAIRPRICE…" |
| `equals` | description or column | `DR` | a column C cell holding exactly "DR" |
| `regex` | description or column | `grab\*?food` | "GRAB\*FOOD", "GRABFOOD" |
| `amount_between` | amount | `0,5` | absolute amounts from 0.00 to 5.00 |
| `is_negative` | amount | — | signed amount below zero |
| `is_positive` | amount | — | signed amount above zero |
| `not_empty` | column | — | the cell in `column_ref` holds any value |

**Sign rule patterns by bank layout** (each profile needs one expense rule and one income rule):

| Bank layout | Expense rule | Income rule |
| --- | --- | --- |
| Separate debit / credit columns (e.g. DBS) | column M `not_empty` | column N `not_empty` |
| Spend shown as positive (typical card export) | amount `is_positive` | amount `is_negative` |
| Spend shown as negative (typical savings export) | amount `is_negative` | amount `is_positive` |
| Indicator column, e.g. C = DR / CR | column C `equals` `DR` | column C `equals` `CR` |

The new-profile form asks "How does this file show spending?" with these four choices and creates the two sign rules; they can be edited like any other rule.

Regex patterns are compiled once when rules load; an invalid regex is rejected by the rule form.

**Seed rules** (from the requirements and the mockup):

| Priority | Action | Condition | Result |
| --- | --- | --- | --- |
| 1 | sign (DBS only) | column M not empty | expense |
| 2 | sign (DBS only) | column N not empty | income |
| 1 | review | description contains `refund` | uncategorised, "Possible refund" |
| 1 | skip | description contains `<YOUR NAME>` | skipped |
| 10 | categorise | description contains `grab*food` | Food |
| 20 | categorise | description contains `grab` | Transport |
| 20 | categorise | description contains `fairprice` | Groceries |
| 20 | categorise | description contains `netflix` | Subscriptions |
| 20 | categorise | description contains `sp services` | Utilities |

Specific rules get lower priority numbers than general ones, which is why `grab*food` (10) is checked before `grab` (20).

**Engine API** (`core/rules.py`):

- `apply_rules(txns: list[Txn], rules: list[Rule], profile_id: int) -> list[Outcome]` — pure, no database access.
- `Outcome(status, category_id, matched_rule_id)`.
- `test_rule(rule, txns) -> list[Txn]` — rows a draft rule would match, for the "Test on this file" button.
- `conflicts(rules, txns) -> list[tuple[Txn, list[Rule]]]` — rows matched by more than one enabled category rule, shown as a warning.

**Make rule from a row.** The suggested pattern is the first word of the description that is at least 3 letters long and not generic (`pos`, `ibg`, `sgp`, `singapore`, `pte`, `ltd`), lower-cased, operator `contains`, priority 20. The user can edit before saving.

## 8. UI specification (single screen)

The whole workflow lives on one Streamlit page, top to bottom, matching the approved mockup: header → categories bar → one card per file → action bar → consolidated result.

**Header.** Session name (editable, default "\<Month YYYY>"), session picker for past sessions, and pills showing files loaded and uncategorised count.

**Categories bar.** Chips with colour dot for each active category; "+ New category" opens a small form (name, colour). Editing or deactivating is done in a sidebar "Manage categories" expander using `st.data_editor`.

**File card** — one per file, in upload order. Finished files collapse to a one-line summary (filename, profile, rows, skipped, uncategorised, file total, Ready/Needs review badge) and expand on click ("Open"). Only one card is open at a time (it holds the shared rule form); an empty upload card follows the cards whenever no draft file exists. The active card (`st.session_state.active_import_id`) shows, in order:

1. **Upload** — `st.file_uploader(type=["csv","xlsx"])` on an empty card. If a profile is detected (section 6), the file imports straight away and the card continues at Preview data; the file line shows filename, row count, profile and the buttons "Edit mapping" and "Remove file". "Edit mapping" needs the file in the uploader (it isn't kept after a page reload).
1b. **Header & columns** — shown instead of the rest of the card when no profile matches, when only the filename matches (pre-filled from that profile), or after "Edit mapping": raw view with the header row highlighted, rows to skip before the header and at the end, sheet (XLSX), column mapping with date format and amount layout, live parse check, and "Save as profile" (disabled until ≥ 90% of rows parse). Full behaviour in section 6.
2. **Preview data** — first 20 normalised rows; column headers show `letter · source header → mapped field`, mapped columns highlighted. A caption shows header row, date format and sign convention. Unparsed rows are listed in an expander.
3. **Rules** (left column) — the add/edit form: column, operator, value, then (category, Skip, Review with a reason, or Sign → expense / income), applies to (all profiles or this profile), priority; buttons "Test on this file" and "Save rule". Below it, the ordered rule list with priority, condition, outcome pill, hit count in this file, and edit/disable controls. Footnote: skip rules run first; lowest number wins.
4. **Rules preview** (right column) — every row of the file with description, amount, result pill (category, Skipped, or a category dropdown + "Make rule" button when unmatched) and which rule decided it. Header counts: matched · skipped · needs a category. Filter toggle: show only uncategorised.
5. **Category result · this file** — a tile per category with its total, uncategorised highlighted in amber, and a dark "File total" tile.

**Review decisions** in the rules preview: a row flagged by a review rule shows its reason ("Possible refund") with **Keep** (opens a category dropdown; a kept refund subtracts from that category) and **Skip**. A `needs_sign` row shows **Expense** / **Income** and a "Save as sign rule" checkbox. Pending rows count toward the "needs a decision" pill, and Consolidate warns with their count and amount.

**Action bar** (below the last card):

- **+ Add new file** — commits the current import (status → `committed`), collapses its card and adds an empty upload card.
- **Consolidate N files** — commits any draft import and shows the consolidated section. Disabled until at least one file is loaded.

**Consolidated result.** Title, list of included files with row and skip counts, the grand total in large type, an amber warning if any uncategorised remains (with amount), a bar row per category (name, bar scaled to the largest category, amount, % of total) sorted descending, a Total row, and "Export XLSX" / "Export CSV" via `st.download_button`.

**State and refresh.** Saving a rule or assigning a category writes to SQLite, re-runs the engine for all imports in the session, then `st.rerun()`. `st.session_state` holds only UI state (active card, expanded cards, consolidated flag); all data is read from SQLite.

## 9. Project structure

UI code only calls `core/` and `db/repo.py`; `core/` never imports Streamlit, so the engine is testable without the UI.

```text
expense_tracker/
  app.py                    # Streamlit page: header, cards, action bar, consolidated view
  ui/
    file_card.py            # upload, preview, rules editor, rules preview, file result
    rule_form.py            # add/edit rule form + validation messages
    profile_form.py         # new/edit profile with live preview
    summary_view.py         # consolidated section + exports
  core/
    models.py               # pydantic: Profile, Category, Rule, Txn, Outcome
    parsers/
      base.py               # Parser protocol: parse(file_bytes, profile) -> DataFrame; registry
      csv_parser.py
      xlsx_parser.py
    profiles.py             # detect_profile(), normalise(), col_letter_to_index()
    mapping.py              # Header & columns step: Mapping, check(), build_profile(), sign_rules_for(), suggestions
    pipeline.py             # import / re-run / save-profile workflow (the one core module using db.repo)
    rules.py                # apply_rules(), test_rule(), conflicts(), suggest_pattern()
    dedupe.py               # row_hash(), mark_duplicates()
    summary.py              # per-file and per-session totals, export frames
  db/
    schema.sql
    migrations/             # 002_*.sql … applied by user_version
    connection.py           # get_conn(): path, PRAGMAs, row_factory, init on first use
    repo.py                 # all SQL: CRUD for profiles/categories/rules, imports, txns
  config/
    seed.yaml               # starter categories, rules, DBS profile
  scripts/
    init_db.py
    seed.py
  tests/
    fixtures/               # dbs_sample.csv, citi_sample.xlsx (fake data)
    test_parsers.py
    test_profiles.py
    test_rules.py
    test_summary.py
  data/                     # expenses.db (git-ignored)
  requirements.txt
  DESIGN.md                 # this document
  CLAUDE.md                 # short build conventions for Claude Code
```

**Suggested `CLAUDE.md` contents:** follow `DESIGN.md`; money as integer cents; no Streamlit imports in `core/`; all SQL in `db/repo.py` with parameterised queries; run `pytest` after each milestone; never commit real bank files, use the fake fixtures.

## 10. Build plan for Claude Code

Build in six milestones, each ending with passing tests and something you can check by hand. Ask Claude Code for one milestone at a time, e.g. "Implement milestone M2 from DESIGN.md".

| # | Milestone | Delivers | Done when |
| --- | --- | --- | --- |
| M1 | Database foundation | Project skeleton, `requirements.txt`, `schema.sql`, `connection.py`, `repo.py` CRUD, `init_db.py`, `seed.py`, `seed.yaml` | Section 3 steps 5–7 work; re-running init and seed changes nothing; repo CRUD tests pass |
| M2 | Parse and normalise | CSV + XLSX parsers, registry, `detect_profile`, `normalise`, letter→index, fake DBS and Citi fixtures | DBS fixture yields correct dates, cents and debit flags; preamble/footer dropped; bad rows flagged unparsed; `col_letter_to_index('L') == 11` |
| M3 | Rule engine | `apply_rules`, all six operators, scope, priority, `test_rule`, `conflicts`, `suggest_pattern` | Seed rules on the DBS fixture: own-name row skipped, refund row flagged for review, GRAB\*FOOD → Food, GRAB → Transport, TOAST BOX uncategorised; credits never counted |
| M4 | Single-file UI | Upload card, profile pick, preview, rule form + list, rules preview with assign/make-rule, file result tiles | Upload DBS fixture, add a rule for TOAST BOX, and the file total updates without reload errors |
| M5 | Multi-file and consolidate | Sessions, collapsed finished cards, Add new file, Consolidate, dedupe across imports, exports | Two fixtures consolidate to the sum of their file totals; re-uploading the same file adds 0 rows; XLSX/CSV export opens in Excel |
| M6 | Profiles UI and polish | New-profile form with live preview, category management, conflict warnings, session picker | A new bank format can be mapped entirely from the UI and imported |

**Test checklist for the reference case** (fake DBS fixture from the mockup):

- [ ] 8 rows parsed; sign rules mark 7 as expense and 1 as income (the refund)
- [ ] FUND TRANSFER TO \<YOUR NAME> skipped; REFUND GRAB\*TRANSPORT shown as uncategorised "Possible refund" with Keep / Skip
- [ ] Utilities 132.60, Groceries 64.25, Food 23.50, Subscriptions 19.98, Transport 18.40, Uncategorised 7.80; file total 266.53 while the refund is pending
- [ ] Keep the refund as Transport → Transport 6.40, file total 254.53; Skip it → total stays 266.53
- [ ] Assigning TOAST BOX to Food via "Make rule" moves 7.80 into Food and leaves the total unchanged
- [ ] A negative-spend copy of the fixture with sign rules `is_negative` → expense gives identical totals
- [ ] A row matching no sign rule shows as needs\_sign and is excluded from totals
- [ ] Consolidating with the Citi fixture (total 660.48) gives 927.01 with the refund pending or skipped

## 11. Future extensions

PDF support is one new parser plus one profile per statement layout; nothing after the parse stage changes.

- **PDF statements:** add `core/parsers/pdf_parser.py` using pdfplumber table extraction, register `.pdf`, and give PDF profiles an optional `page_range` and a `post_process` hook (e.g. join wrapped description lines). The `file_type` CHECK already allows `'pdf'`.
- **Multiple currencies:** add `currency` to `transactions` and an FX table; summaries group by currency or convert to SGD.
- **Trends:** month-by-month totals per category across sessions, since every session is kept in SQLite.
- **Smarter suggestions:** suggest a category for unmatched rows from past manual assignments of the same merchant word.
