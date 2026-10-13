"""Import workflow: parse -> detect -> normalise -> store -> apply rules, and re-runs.

This is the glue the UI calls. It is the one core module that touches the
database (through db.repo); the stages it calls stay pure. No Streamlit here.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import asdict, dataclass

import pandas as pd

from core.dedupe import apply_duplicates, mark_duplicates
from core.models import Outcome, Profile, Rule, StoredTxn
from core.parsers import file_type_of, parse_file
from core.profiles import detect, normalise
from core.rules import apply_rules, assign_category, choose_sign, ordered, skip_row
from core.summary import FileLine, FileSummary, SessionSummary, build_session_summary, summarise
from db import repo


@dataclass
class Upload:
    filename: str
    sha256: str
    file_type: str
    raw: pd.DataFrame  # read with default settings (first sheet)
    detected: Profile | None  # filename and header signature match: import straight away
    near_miss: Profile | None  # filename matches, header doesn't: open the mapping step pre-filled


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_upload(conn: sqlite3.Connection, filename: str, data: bytes) -> Upload:
    """Read the file and detect its profile. Raises ParseError for unreadable files."""
    raw = parse_file(filename, data)
    detection = detect(filename, repo.list_profiles(conn), lambda profile: parse_file(filename, data, profile))
    return Upload(filename, sha256_of(data), file_type_of(filename), raw, detection.profile, detection.near_miss)


def save_profile_and_import(
    conn: sqlite3.Connection, *, session_id: int, filename: str, data: bytes,
    profile: Profile, sign_rules: list[Rule], replace_import_id: int | None = None,
) -> tuple[int, int]:
    """Save a profile from the Header & columns step, then import the file with it.

    Replaces the profile's sign rules, imports the file as a new draft (deleting
    `replace_import_id`, the same file's earlier import), and re-runs the session
    so other files using this profile follow the new sign rules.
    Returns (profile_id, import_id).
    """
    profile_id = repo.save_profile_with_sign_rules(conn, profile, sign_rules)
    if replace_import_id is not None:
        repo.delete_import(conn, replace_import_id)  # first, so the new copy isn't its duplicate
    import_id = import_file(conn, session_id, filename, data, profile_id)
    rerun_session(conn, session_id)
    return profile_id, import_id


def import_file(conn: sqlite3.Connection, session_id: int, filename: str, data: bytes, profile_id: int) -> int:
    """Normalise a file with a profile, decide every row, and store it as a draft import.

    Raises ParseError / ProfileError when the file doesn't fit the profile.
    """
    profile = repo.get_profile(conn, profile_id)
    if profile is None:
        raise LookupError(f"profile {profile_id} not found")
    rules = repo.list_rules(conn, profile_id=profile.id)
    result = normalise(parse_file(filename, data, profile), profile, rules)
    outcomes = apply_rules(result.txns, rules, profile.id, headers=result.headers)
    existing = repo.committed_hashes(conn, (t.row_hash for t in result.txns))
    outcomes = apply_duplicates(outcomes, mark_duplicates(result.txns, existing))
    return repo.create_import(
        conn,
        session_id=session_id,
        profile_id=profile.id,
        filename=filename,
        file_sha256=sha256_of(data),
        row_count=len(result.txns),
        headers=result.headers,
        unparsed=[asdict(u) for u in result.unparsed],
        rows=zip(result.txns, outcomes),
    )


def rerun_import(conn: sqlite3.Connection, import_id: int) -> int:
    """Re-apply current rules and duplicate checks to one import, keeping user decisions.

    Duplicates are recomputed every time against committed imports created before
    this one, so removing an earlier import brings its copies back. Returns rows changed.
    """
    imp = repo.get_import(conn, import_id)
    if imp is None:
        raise LookupError(f"import {import_id} not found")
    stored = repo.list_transactions(conn, import_id)
    txns = [s.txn for s in stored]
    rules = repo.list_rules(conn, profile_id=imp["profile_id"])
    previous = [None if s.outcome.status == "duplicate" else s.outcome for s in stored]
    outcomes = apply_rules(txns, rules, imp["profile_id"], previous=previous, headers=imp["headers"])
    existing = repo.committed_hashes(conn, (t.row_hash for t in txns), before_import_id=import_id)
    outcomes = apply_duplicates(outcomes, mark_duplicates(txns, existing))
    changed = [(s.id, o) for s, o in zip(stored, outcomes) if o != s.outcome]
    return repo.update_outcomes(conn, changed)


def rerun_session(conn: sqlite3.Connection, session_id: int) -> int:
    """Re-apply rules to every import of the session, in upload order (after any change)."""
    return sum(rerun_import(conn, imp["id"]) for imp in repo.list_imports(conn, session_id))


def file_summary(conn: sqlite3.Connection, import_id: int) -> FileSummary:
    return summarise(repo.list_transactions(conn, import_id), repo.list_categories(conn))


# ---------------------------------------------------------------- session: commit, consolidate, remove

def commit(conn: sqlite3.Connection, import_id: int) -> None:
    """Mark a draft import committed ('+ Add new file'); later imports re-check duplicates."""
    imp = repo.get_import(conn, import_id)
    if imp is None:
        raise LookupError(f"import {import_id} not found")
    if imp["status"] != "committed":
        repo.commit_import(conn, import_id)
        rerun_session(conn, imp["session_id"])


def remove_import(conn: sqlite3.Connection, import_id: int) -> None:
    """Delete an import and re-check the session's duplicates."""
    imp = repo.get_import(conn, import_id)
    if imp is None:
        return
    repo.delete_import(conn, import_id)
    rerun_session(conn, imp["session_id"])


def session_summary(conn: sqlite3.Connection, session_id: int) -> SessionSummary:
    """Consolidated totals from the summary query, plus per-file lines and pending counts."""
    profiles = {p.id: p.name for p in repo.list_profiles(conn)}
    files = []
    for imp in repo.list_imports(conn, session_id):
        files.append(FileLine(
            import_id=imp["id"], filename=imp["filename"], profile=profiles.get(imp["profile_id"], "?"),
            status=imp["status"], summary=file_summary(conn, imp["id"])))
    return build_session_summary(repo.session_category_totals(conn, session_id), files)


def consolidate(conn: sqlite3.Connection, session_id: int) -> SessionSummary:
    """'Consolidate N files': commit every draft import, stamp the session, return the totals."""
    for imp in repo.list_imports(conn, session_id):
        if imp["status"] == "draft":
            repo.commit_import(conn, imp["id"])
    rerun_session(conn, session_id)
    repo.mark_session_consolidated(conn, session_id)
    return session_summary(conn, session_id)


def session_transactions(conn: sqlite3.Connection, session_id: int) -> list[tuple[str, StoredTxn]]:
    """(filename, row) for every transaction of the session, for the export."""
    return [(imp["filename"], row)
            for imp in repo.list_imports(conn, session_id)
            for row in repo.list_transactions(conn, imp["id"])]


# ---------------------------------------------------------------- user decisions on one row

def _stored_or_raise(conn: sqlite3.Connection, txn_id: int):
    stored = repo.get_transaction(conn, txn_id)
    if stored is None:
        raise LookupError(f"transaction {txn_id} not found")
    return stored


def assign(conn: sqlite3.Connection, txn_id: int, category_id: int) -> None:
    """Assign once (or Keep a review row) as this category."""
    stored = _stored_or_raise(conn, txn_id)
    repo.update_outcomes(conn, [(txn_id, assign_category(stored.outcome, category_id))])


def skip(conn: sqlite3.Connection, txn_id: int) -> None:
    stored = _stored_or_raise(conn, txn_id)
    repo.update_outcomes(conn, [(txn_id, skip_row(stored.outcome))])


def reset(conn: sqlite3.Connection, txn_id: int) -> None:
    """Undo a user decision: rules decide the row again."""
    stored = _stored_or_raise(conn, txn_id)
    if stored.outcome.decided_by == "user":
        cleared = Outcome(status="needs_sign", decided_by="default")  # placeholder, re-decided below
        repo.update_outcomes(conn, [(txn_id, cleared)])
        rerun_import(conn, stored.import_id)


def set_sign(conn: sqlite3.Connection, txn_id: int, is_debit: bool, *, save_rule: bool = False) -> Rule | None:
    """Expense/Income on a needs_sign row.

    With save_rule, adds a sign rule for this profile from the row's amount sign
    (negative/positive amount -> the chosen direction), then re-runs the session so
    every matching row follows it. Returns the saved rule, if any.
    """
    stored = _stored_or_raise(conn, txn_id)
    imp = repo.get_import(conn, stored.import_id)
    profile_id = imp["profile_id"]
    signed = stored.txn.signed_amount_cents
    if save_rule and signed != 0:
        existing = ordered(repo.list_rules(conn, action="sign", profile_id=profile_id), "sign", profile_id)
        rule = Rule(
            action="sign", sign_result="expense" if is_debit else "income", target_field="amount",
            operator="is_negative" if signed < 0 else "is_positive", profile_id=profile_id,
            priority=max((r.priority for r in existing), default=0) + 1,
        )
        rule = rule.model_copy(update={"id": repo.create_rule(conn, rule)})
        rerun_session(conn, imp["session_id"])
        return rule
    rules = repo.list_rules(conn, profile_id=profile_id)
    outcome = choose_sign(stored.txn, is_debit, rules, profile_id, imp["headers"])
    repo.update_outcomes(conn, [(txn_id, outcome)])
    return None


def save_rule(conn: sqlite3.Connection, rule: Rule, session_id: int) -> int:
    """Create or update a rule, then re-run the session. Returns the rule id."""
    if rule.id is None:
        rule_id = repo.create_rule(conn, rule)
    else:
        repo.update_rule(conn, rule)
        rule_id = rule.id
    rerun_session(conn, session_id)
    return rule_id
