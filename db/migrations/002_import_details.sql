-- Keep what the UI needs to show an import after a page reload, and to resolve
-- rules that name a column by its header when rules re-run.
ALTER TABLE imports ADD COLUMN headers  TEXT;  -- JSON: {"A": "Transaction Date", ...}
ALTER TABLE imports ADD COLUMN unparsed TEXT;  -- JSON: [{"row_number": 9, "reason": "...", "raw_row": {...}}]
