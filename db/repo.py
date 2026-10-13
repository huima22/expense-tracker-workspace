"""All data SQL for the app. Every value is a bound parameter; each write commits.

Table and column names in the f-strings below come from constants in this
module, never from user input.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from core.models import Category, Outcome, Profile, Rule, StoredTxn, Txn

Conn = sqlite3.Connection


# ---------------------------------------------------------------- helpers

def _insert(conn: Conn, table: str, values: dict[str, Any]) -> int:
    cols = ", ".join(values)
    marks = ", ".join(f":{c}" for c in values)
    with conn:
        cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", values)
    return cur.lastrowid


def _update(conn: Conn, table: str, row_id: int, values: dict[str, Any]) -> None:
    sets = ", ".join(f"{c} = :{c}" for c in values)
    with conn:
        cur = conn.execute(f"UPDATE {table} SET {sets} WHERE id = :id", {**values, "id": row_id})
    if cur.rowcount == 0:
        raise LookupError(f"{table} id {row_id} not found")


def _delete(conn: Conn, table: str, row_id: int) -> bool:
    with conn:
        cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    return cur.rowcount > 0


def _require_id(model: Category | Profile | Rule) -> int:
    if model.id is None:
        raise ValueError(f"{type(model).__name__} has no id; create it first")
    return model.id


def _revalidate(model):
    """Re-run validators, since pydantic doesn't validate fields changed after creation."""
    return type(model).model_validate(model.model_dump())


# ---------------------------------------------------------------- categories

_CATEGORY_COLS = "id, name, color, sort_order, is_active"


def _category_values(c: Category) -> dict[str, Any]:
    c = _revalidate(c)
    return {"name": c.name, "color": c.color, "sort_order": c.sort_order, "is_active": int(c.is_active)}


def create_category(conn: Conn, category: Category) -> int:
    return _insert(conn, "categories", _category_values(category))


def get_category(conn: Conn, category_id: int) -> Category | None:
    row = conn.execute(f"SELECT {_CATEGORY_COLS} FROM categories WHERE id = ?", (category_id,)).fetchone()
    return Category.model_validate(dict(row)) if row else None


def get_category_by_name(conn: Conn, name: str) -> Category | None:
    row = conn.execute(f"SELECT {_CATEGORY_COLS} FROM categories WHERE name = ?", (name,)).fetchone()
    return Category.model_validate(dict(row)) if row else None


def list_categories(conn: Conn, *, active_only: bool = False) -> list[Category]:
    where = "WHERE is_active = 1" if active_only else ""
    rows = conn.execute(f"SELECT {_CATEGORY_COLS} FROM categories {where} ORDER BY sort_order, name")
    return [Category.model_validate(dict(r)) for r in rows]


def update_category(conn: Conn, category: Category) -> None:
    _update(conn, "categories", _require_id(category), _category_values(category))


def delete_category(conn: Conn, category_id: int) -> bool:
    """Delete a category. Its categorise rules are deleted too (ON DELETE CASCADE)."""
    return _delete(conn, "categories", category_id)


def upsert_category(conn: Conn, category: Category) -> int:
    existing = get_category_by_name(conn, category.name)
    if existing is None:
        return create_category(conn, category)
    update_category(conn, category.model_copy(update={"id": existing.id}))
    return existing.id


# ---------------------------------------------------------------- profiles

_PROFILE_COLS = (
    "id, name, file_type, filename_pattern, header_row, header_signature, "
    "skip_footer_rows, sheet_name, column_map, date_format, encoding"
)


def _profile_values(p: Profile) -> dict[str, Any]:
    p = _revalidate(p)
    return {
        "name": p.name,
        "file_type": p.file_type,
        "filename_pattern": p.filename_pattern,
        "header_row": p.header_row,
        "header_signature": json.dumps(p.header_signature) if p.header_signature is not None else None,
        "skip_footer_rows": p.skip_footer_rows,
        "sheet_name": p.sheet_name,
        "column_map": p.column_map.model_dump_json(),
        "date_format": p.date_format,
        "encoding": p.encoding,
    }


def _profile(row: sqlite3.Row) -> Profile:
    data = dict(row)
    data["column_map"] = json.loads(data["column_map"])
    if data["header_signature"] is not None:
        data["header_signature"] = json.loads(data["header_signature"])
    return Profile.model_validate(data)


def create_profile(conn: Conn, profile: Profile) -> int:
    return _insert(conn, "profiles", _profile_values(profile))


def get_profile(conn: Conn, profile_id: int) -> Profile | None:
    row = conn.execute(f"SELECT {_PROFILE_COLS} FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    return _profile(row) if row else None


def get_profile_by_name(conn: Conn, name: str) -> Profile | None:
    row = conn.execute(f"SELECT {_PROFILE_COLS} FROM profiles WHERE name = ?", (name,)).fetchone()
    return _profile(row) if row else None


def list_profiles(conn: Conn) -> list[Profile]:
    return [_profile(r) for r in conn.execute(f"SELECT {_PROFILE_COLS} FROM profiles ORDER BY name")]


def update_profile(conn: Conn, profile: Profile) -> None:
    _update(conn, "profiles", _require_id(profile), _profile_values(profile))


def delete_profile(conn: Conn, profile_id: int) -> bool:
    """Delete a profile and its rules. Fails with IntegrityError if imports use it."""
    return _delete(conn, "profiles", profile_id)


def save_profile_with_sign_rules(conn: Conn, profile: Profile, sign_rules: Iterable[Rule]) -> int:
    """Create (id None) or update a profile and replace its sign rules, in one transaction.

    Each rule's profile_id is set to the saved profile. Raises IntegrityError if the
    name is taken by another profile, LookupError if the profile to update is gone.
    """
    values = _profile_values(profile)
    with conn:
        if profile.id is None:
            cols = ", ".join(values)
            marks = ", ".join(f":{c}" for c in values)
            profile_id = conn.execute(f"INSERT INTO profiles ({cols}) VALUES ({marks})", values).lastrowid
        else:
            profile_id = profile.id
            sets = ", ".join(f"{c} = :{c}" for c in values)
            cur = conn.execute(f"UPDATE profiles SET {sets} WHERE id = :id", {**values, "id": profile_id})
            if cur.rowcount == 0:
                raise LookupError(f"profiles id {profile_id} not found")
        conn.execute("DELETE FROM rules WHERE profile_id = ? AND action = 'sign'", (profile_id,))
        for rule in sign_rules:
            rule_values = _rule_values(rule.model_copy(update={"profile_id": profile_id, "id": None}))
            cols = ", ".join(rule_values)
            marks = ", ".join(f":{c}" for c in rule_values)
            conn.execute(f"INSERT INTO rules ({cols}) VALUES ({marks})", rule_values)
    return profile_id


def upsert_profile(conn: Conn, profile: Profile) -> int:
    existing = get_profile_by_name(conn, profile.name)
    if existing is None:
        return create_profile(conn, profile)
    update_profile(conn, profile.model_copy(update={"id": existing.id}))
    return existing.id


# ---------------------------------------------------------------- rules

_RULE_COLS = (
    "id, action, sign_result, category_id, target_field, column_ref, operator, "
    "pattern, profile_id, priority, enabled, note"
)
# Evaluation order from DESIGN.md section 7: sign, review, skip, categorise.
_RULE_ORDER = (
    "CASE action WHEN 'sign' THEN 0 WHEN 'review' THEN 1 WHEN 'skip' THEN 2 ELSE 3 END, "
    "priority, id"
)


def _rule_values(r: Rule) -> dict[str, Any]:
    r = _revalidate(r)
    values = r.model_dump(exclude={"id"})
    values["enabled"] = int(r.enabled)
    return values


def create_rule(conn: Conn, rule: Rule) -> int:
    return _insert(conn, "rules", _rule_values(rule))


def get_rule(conn: Conn, rule_id: int) -> Rule | None:
    row = conn.execute(f"SELECT {_RULE_COLS} FROM rules WHERE id = ?", (rule_id,)).fetchone()
    return Rule.model_validate(dict(row)) if row else None


def list_rules(
    conn: Conn,
    *,
    action: str | None = None,
    profile_id: int | None = None,
    enabled_only: bool = False,
) -> list[Rule]:
    """Rules in evaluation order. With profile_id: global rules plus that profile's rules."""
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if action is not None:
        clauses.append("action = :action")
        params["action"] = action
    if profile_id is not None:
        clauses.append("(profile_id IS NULL OR profile_id = :profile_id)")
        params["profile_id"] = profile_id
    if enabled_only:
        clauses.append("enabled = 1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT {_RULE_COLS} FROM rules {where} ORDER BY {_RULE_ORDER}", params)
    return [Rule.model_validate(dict(r)) for r in rows]


def update_rule(conn: Conn, rule: Rule) -> None:
    _update(conn, "rules", _require_id(rule), _rule_values(rule))


def set_rule_enabled(conn: Conn, rule_id: int, enabled: bool) -> None:
    _update(conn, "rules", rule_id, {"enabled": int(enabled)})


def delete_rule(conn: Conn, rule_id: int) -> bool:
    return _delete(conn, "rules", rule_id)


def find_rule(conn: Conn, rule: Rule) -> int | None:
    """Id of a rule with the same condition (action, field, column, operator, pattern, profile)."""
    row = conn.execute(
        """SELECT id FROM rules
           WHERE action = :action AND target_field = :target_field
             AND column_ref IS :column_ref AND operator = :operator
             AND pattern IS :pattern AND profile_id IS :profile_id
           ORDER BY id LIMIT 1""",
        rule.model_dump(include={"action", "target_field", "column_ref", "operator", "pattern", "profile_id"}),
    ).fetchone()
    return row["id"] if row else None


def upsert_rule(conn: Conn, rule: Rule) -> int:
    """Insert, or update the rule with the same condition (its outcome, priority, note)."""
    existing_id = find_rule(conn, rule)
    if existing_id is None:
        return create_rule(conn, rule)
    update_rule(conn, rule.model_copy(update={"id": existing_id}))
    return existing_id


# ---------------------------------------------------------------- sessions

_SESSION_COLS = "id, name, created_at, consolidated_at"


def _session_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("session name must not be empty")
    return name


def create_session(conn: Conn, name: str) -> int:
    return _insert(conn, "sessions", {"name": _session_name(name)})


def get_session(conn: Conn, session_id: int) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {_SESSION_COLS} FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def list_sessions(conn: Conn) -> list[dict[str, Any]]:
    """Newest first."""
    rows = conn.execute(f"SELECT {_SESSION_COLS} FROM sessions ORDER BY created_at DESC, id DESC")
    return [dict(r) for r in rows]


def rename_session(conn: Conn, session_id: int, name: str) -> None:
    _update(conn, "sessions", session_id, {"name": _session_name(name)})


def mark_session_consolidated(conn: Conn, session_id: int) -> None:
    with conn:
        cur = conn.execute(
            "UPDATE sessions SET consolidated_at = datetime('now') WHERE id = ?", (session_id,)
        )
    if cur.rowcount == 0:
        raise LookupError(f"sessions id {session_id} not found")


def delete_session(conn: Conn, session_id: int) -> bool:
    """Delete a session with its imports and transactions (ON DELETE CASCADE)."""
    return _delete(conn, "sessions", session_id)


# ---------------------------------------------------------------- imports

_IMPORT_COLS = (
    "id, session_id, profile_id, filename, file_sha256, row_count, status, imported_at, headers, unparsed"
)


def _import(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["headers"] = json.loads(data["headers"]) if data["headers"] else {}
    data["unparsed"] = json.loads(data["unparsed"]) if data["unparsed"] else []
    return data


def create_import(
    conn: Conn,
    *,
    session_id: int,
    profile_id: int,
    filename: str,
    file_sha256: str,
    row_count: int,
    headers: dict[str, str] | None = None,
    unparsed: list[dict[str, Any]] | None = None,
    rows: Iterable[tuple[Txn, Outcome]] = (),
) -> int:
    """Create a draft import and its transactions in one database transaction."""
    values = {
        "session_id": session_id,
        "profile_id": profile_id,
        "filename": filename,
        "file_sha256": file_sha256,
        "row_count": row_count,
        "headers": json.dumps(headers) if headers is not None else None,
        "unparsed": json.dumps(unparsed) if unparsed is not None else None,
    }
    cols = ", ".join(values)
    marks = ", ".join(f":{c}" for c in values)
    with conn:
        import_id = conn.execute(f"INSERT INTO imports ({cols}) VALUES ({marks})", values).lastrowid
        conn.executemany(_INSERT_TXN, [_txn_values(import_id, t, o) for t, o in rows])
    return import_id


def get_import(conn: Conn, import_id: int) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {_IMPORT_COLS} FROM imports WHERE id = ?", (import_id,)).fetchone()
    return _import(row) if row else None


def list_imports(conn: Conn, session_id: int) -> list[dict[str, Any]]:
    """Imports of a session in upload order."""
    rows = conn.execute(
        f"SELECT {_IMPORT_COLS} FROM imports WHERE session_id = ? ORDER BY id", (session_id,)
    )
    return [_import(r) for r in rows]


def commit_import(conn: Conn, import_id: int) -> None:
    _update(conn, "imports", import_id, {"status": "committed"})


def delete_import(conn: Conn, import_id: int) -> bool:
    """Delete an import and its transactions (ON DELETE CASCADE)."""
    return _delete(conn, "imports", import_id)


# ---------------------------------------------------------------- transactions

_TXN_COLS = (
    "id, import_id, row_hash, txn_date, description, signed_amount_cents, amount_cents, is_debit, "
    "status, review_reason, decided_by, category_id, matched_rule_id, raw_row"
)
_INSERT_TXN = (
    "INSERT INTO transactions (import_id, row_hash, txn_date, description, signed_amount_cents, "
    "amount_cents, is_debit, status, review_reason, decided_by, category_id, matched_rule_id, raw_row) "
    "VALUES (:import_id, :row_hash, :txn_date, :description, :signed_amount_cents, :amount_cents, "
    ":is_debit, :status, :review_reason, :decided_by, :category_id, :matched_rule_id, :raw_row)"
)


def _bool_or_none(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _outcome_values(o: Outcome) -> dict[str, Any]:
    return {
        "is_debit": _bool_or_none(o.is_debit),
        "status": o.status,
        "review_reason": o.review_reason,
        "decided_by": o.decided_by,
        "category_id": o.category_id,
        "matched_rule_id": o.matched_rule_id,
    }


def _txn_values(import_id: int, t: Txn, o: Outcome) -> dict[str, Any]:
    return {
        "import_id": import_id,
        "row_hash": t.row_hash,
        "txn_date": t.txn_date.isoformat(),
        "description": t.description,
        "signed_amount_cents": t.signed_amount_cents,
        "amount_cents": t.amount_cents,
        "raw_row": json.dumps(t.raw_row),
        **_outcome_values(o),
    }


def _stored(row: sqlite3.Row) -> StoredTxn:
    is_debit = None if row["is_debit"] is None else bool(row["is_debit"])
    status, decided_by = row["status"], row["decided_by"]
    if status == "categorised" and row["category_id"] is None:
        status, decided_by = "uncategorised", "default"  # its category was deleted (ON DELETE SET NULL)
    txn = Txn(
        txn_date=row["txn_date"],
        description=row["description"],
        signed_amount_cents=row["signed_amount_cents"],
        amount_cents=row["amount_cents"],
        is_debit=is_debit,
        row_hash=row["row_hash"],
        raw_row=json.loads(row["raw_row"]),
    )
    outcome = Outcome(
        status=status,
        is_debit=is_debit,
        category_id=row["category_id"],
        matched_rule_id=row["matched_rule_id"],
        review_reason=row["review_reason"],
        decided_by=decided_by,
    )
    return StoredTxn(id=row["id"], import_id=row["import_id"], txn=txn, outcome=outcome)


def insert_transactions(conn: Conn, import_id: int, rows: Iterable[tuple[Txn, Outcome]]) -> None:
    with conn:
        conn.executemany(_INSERT_TXN, [_txn_values(import_id, t, o) for t, o in rows])


def list_transactions(conn: Conn, import_id: int) -> list[StoredTxn]:
    """Transactions of an import in file order."""
    rows = conn.execute(f"SELECT {_TXN_COLS} FROM transactions WHERE import_id = ? ORDER BY id", (import_id,))
    return [_stored(r) for r in rows]


def get_transaction(conn: Conn, txn_id: int) -> StoredTxn | None:
    row = conn.execute(f"SELECT {_TXN_COLS} FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    return _stored(row) if row else None


_HASH_CHUNK = 500  # stay well under SQLite's bound-parameter limit


def committed_hashes(conn: Conn, hashes: Iterable[str], *, before_import_id: int | None = None) -> set[str]:
    """Which of these row hashes already exist in a committed import.

    With before_import_id, only imports created before it (lower id) count, so the
    earliest import of a transaction keeps it and later copies are the duplicates.
    """
    wanted = list(dict.fromkeys(hashes))
    found: set[str] = set()
    for start in range(0, len(wanted), _HASH_CHUNK):
        chunk = wanted[start:start + _HASH_CHUNK]
        marks = ", ".join("?" for _ in chunk)
        rows = conn.execute(
            f"""SELECT DISTINCT t.row_hash FROM transactions t
                JOIN imports i ON i.id = t.import_id
                WHERE i.status = 'committed' AND (? IS NULL OR i.id < ?) AND t.row_hash IN ({marks})""",
            [before_import_id, before_import_id, *chunk],
        )
        found.update(r["row_hash"] for r in rows)
    return found


def session_category_totals(conn: Conn, session_id: int) -> list[dict[str, Any]]:
    """The consolidated summary query (DESIGN.md section 5), in cents.

    Expenses add, kept refunds (income the user chose to Keep) subtract from their
    category; pending items (needs_sign, uncategorised income) are left out.
    Rows: {category_id (None = Uncategorised), category, color, cents}, largest first.
    """
    rows = conn.execute(
        """SELECT c.id AS category_id,
                  COALESCE(c.name, 'Uncategorised') AS category,
                  COALESCE(c.color, '#9AA3AE') AS color,
                  SUM(CASE WHEN t.is_debit = 1 THEN t.amount_cents ELSE -t.amount_cents END) AS cents
           FROM transactions t
           JOIN imports i         ON i.id = t.import_id
           LEFT JOIN categories c ON c.id = t.category_id
           WHERE i.session_id = :session_id
             AND (   (t.status = 'categorised' AND t.category_id IS NOT NULL)
                  OR (t.status = 'uncategorised' AND t.is_debit = 1))
           GROUP BY c.id
           ORDER BY cents DESC, category""",
        {"session_id": session_id},
    )
    return [dict(r) for r in rows]


def update_outcomes(conn: Conn, updates: Iterable[tuple[int, Outcome]]) -> int:
    """Write new outcomes for transactions by id; returns how many rows changed."""
    params = [{**_outcome_values(o), "id": txn_id} for txn_id, o in updates]
    sets = ", ".join(f"{c} = :{c}" for c in _outcome_values(Outcome(status="needs_sign")))
    with conn:
        conn.executemany(f"UPDATE transactions SET {sets} WHERE id = :id", params)
    return len(params)
