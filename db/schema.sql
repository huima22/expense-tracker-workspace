-- Schema version 1 (DESIGN.md section 5). Later changes go in db/migrations/NNN_*.sql.
-- Money is integer cents; dates are ISO YYYY-MM-DD text.

CREATE TABLE IF NOT EXISTS profiles (
  id               INTEGER PRIMARY KEY,
  name             TEXT NOT NULL UNIQUE,          -- 'DBS Savings CSV'
  file_type        TEXT NOT NULL CHECK (file_type IN ('csv','xlsx','pdf')),
  filename_pattern TEXT,                          -- regex, e.g. '(?i)^dbs.*\.csv$'
  header_row       INTEGER NOT NULL DEFAULT 1,    -- 1-based row holding column headers
  header_signature TEXT,                          -- JSON: {"A":"Transaction Date","L":"Description"}
  skip_footer_rows INTEGER NOT NULL DEFAULT 0,
  sheet_name       TEXT,                          -- xlsx only; NULL = first sheet
  column_map       TEXT NOT NULL,                 -- JSON, see DESIGN.md section 6
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
