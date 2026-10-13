"""File cards: one per import in upload order, plus an empty upload card when no draft exists.

The active card (st.session_state.active_import_id) is open: upload, header &
columns, preview, rules editor, rules preview, file result. Every other card is
collapsed to a one-line summary with an Open button. Only one card is open at a
time because the rule form's widgets are shared.

Everything shown is read from SQLite; st.session_state only holds UI state
(active import, uploader generation, form fields).
"""
from __future__ import annotations

import sqlite3
from html import escape

import pandas as pd
import streamlit as st

from core import pipeline
from core.columns import resolve_column
from core.models import Category, Profile, Rule, StoredTxn
from core.parsers import ParseError
from core.profiles import ProfileError
from core.rules import describe_condition, ordered, suggest_pattern
from core.summary import FileSummary
from db import repo
from ui import profile_form, rule_form
from ui.common import html, money, pill, result_tiles

_MAPPED_BG = "background-color: rgba(59,130,246,.12)"


# ---------------------------------------------------------------- which card is open

NO_CARD = 0  # active_import_id value meaning "all cards collapsed"


def draft_import(imports: list[dict]) -> dict | None:
    """The file being worked on (at most one: '+ Add new file' commits it)."""
    return next((i for i in reversed(imports) if i["status"] == "draft"), None)


def active_id(imports: list[dict]) -> int | None:
    """The open card: the remembered one, else the draft. None = all collapsed."""
    s = st.session_state
    remembered = s.get("active_import_id")
    if remembered == NO_CARD:
        return None
    if remembered in {i["id"] for i in imports}:
        return remembered
    draft = draft_import(imports)
    s["active_import_id"] = draft["id"] if draft else None
    return s["active_import_id"]


def open_card(import_id: int | None) -> None:
    """Make a card the open one (NO_CARD collapses all); resets the rule form."""
    st.session_state["active_import_id"] = import_id
    rule_form.request_prefill(None)


def new_uploader() -> None:
    """A fresh, empty uploader on the next run."""
    s = st.session_state
    s["uploader_gen"] = s.get("uploader_gen", 0) + 1


def _remove_import(conn: sqlite3.Connection, imp: dict) -> None:
    pipeline.remove_import(conn, imp["id"])
    open_card(None)
    if imp["status"] == "draft":
        new_uploader()


# ---------------------------------------------------------------- 1. upload and 1b. header & columns

def _upload(conn: sqlite3.Connection, session_id: int, imp: dict | None) -> tuple[bool, bool]:
    """Upload, then import straight away if a profile matches, else the Header & columns step.

    Returns (show_card, file_available): whether the active import's card should be
    drawn below, and whether its file is in the uploader (needed to Edit mapping).
    """
    s = st.session_state
    st.markdown("#### Upload")
    file = st.file_uploader("Bank file (CSV or XLSX)", type=["csv", "xlsx"],
                            key=f"uploader_{s.get('uploader_gen', 0)}")
    if file is None:
        if imp is not None:
            st.caption("The file itself isn't kept after a page reload; upload it again to edit its mapping.")
        return imp is not None, False

    data = file.getvalue()
    try:
        upload = pipeline.read_upload(conn, file.name, data)
    except ParseError as exc:
        st.error(f"Can't read {file.name}: {exc}")
        return False, False

    same_file = imp is not None and imp["file_sha256"] == upload.sha256
    editing = same_file and s.get("hc_edit") == upload.sha256
    if same_file and not editing:
        return True, True

    base = None
    if editing:
        base = repo.get_profile(conn, imp["profile_id"])
    elif upload.detected is not None:
        try:
            new_id = pipeline.import_file(conn, session_id, file.name, data, upload.detected.id)
        except (ParseError, ProfileError) as exc:
            st.warning(f"'{upload.detected.name}' matched this file but couldn't read it ({exc}). "
                       "Check the settings below.")
            base = upload.detected
        else:
            if imp is not None:
                repo.delete_import(conn, imp["id"])
            s["active_import_id"] = new_id
            s["flash"] = f"Recognised {file.name} as '{upload.detected.name}'."
            rule_form.request_prefill(None)
            st.rerun()
    else:
        base = upload.near_miss

    profile_form.render(conn, session_id=session_id, filename=file.name, data=data,
                        file_type=upload.file_type, sha256=upload.sha256, base=base, editing=editing,
                        replace_import_id=imp["id"] if imp is not None else None)
    return False, True


def _file_line(conn: sqlite3.Connection, imp: dict, profile: Profile, summary: FileSummary,
               file_available: bool) -> None:
    c1, c2, c3, c4 = st.columns([4.2, 1.1, 1, 0.9], vertical_alignment="center")
    status = pill("draft") if imp["status"] == "draft" else pill("committed", "green")
    c1.markdown(f"**{escape(imp['filename'])}** · {imp['row_count']} rows · profile **{escape(profile.name)}** "
                f"{status}", unsafe_allow_html=True)
    can_edit = file_available and imp["status"] == "draft"
    help_text = None if can_edit else (
        "Upload the file again to edit its mapping." if imp["status"] == "draft"
        else "Committed files keep their mapping; remove the file and upload it again to change it.")
    if c2.button("Edit mapping", key="edit_mapping", width="stretch", disabled=not can_edit, help=help_text):
        st.session_state["hc_edit"] = imp["file_sha256"]
        st.rerun()
    if c3.button("Remove file", key="remove_file", width="stretch"):
        _remove_import(conn, imp)
        st.rerun()
    if c4.button("Collapse", key="collapse_card", width="stretch"):
        open_card(NO_CARD)
        st.rerun()
    if summary.duplicates:
        what = "All" if summary.duplicates == summary.rows else str(summary.duplicates)
        st.info(f"{what} of this file's {summary.rows} rows were already imported in a committed file; "
                "they're marked Duplicate and not counted.")


def _collapsed_card(imp: dict, profile_name: str, summary: FileSummary) -> None:
    """One-line summary: filename, profile, rows, skipped, uncategorised, file total, badge."""
    with st.container(border=True):
        c1, c2, c3 = st.columns([5.2, 1.6, 0.9], vertical_alignment="center")
        open_rows = summary.needs_category + summary.pending
        parts = [f"**{escape(imp['filename'])}**", escape(profile_name), f"{summary.rows} rows",
                 f"{summary.skipped} skipped", f"{open_rows} uncategorised"]
        if summary.duplicates:
            parts.append(f"{summary.duplicates} duplicates")
        draft = " " + pill("draft") if imp["status"] == "draft" else ""
        c1.markdown(" · ".join(parts) + draft, unsafe_allow_html=True)
        badge = pill("Ready", "green") if summary.ready else pill("Needs review", "amber")
        c2.markdown(f"<div class='amount'><b>{money(summary.total_cents)}</b> {badge}</div>",
                    unsafe_allow_html=True)
        if c3.button("Open", key=f"open_{imp['id']}", width="stretch"):
            open_card(imp["id"])
            st.rerun()


# ---------------------------------------------------------------- 2. preview

def _sign_convention(rules: list[Rule], headers: dict[str, str]) -> str:
    signs = ordered(rules, "sign")
    if not signs:
        return "no sign rules: every row needs a decision"
    return "; ".join(f"{describe_condition(r, headers)} → {r.sign_result}" for r in signs)


def _preview(imp: dict, profile: Profile, stored: list[StoredTxn], rules: list[Rule]) -> None:
    headers = imp["headers"]
    st.markdown("#### Preview data")
    cmap = profile.column_map
    field_of: dict[str, str] = {}
    for name in ("date", "description", "amount", "debit", "credit"):
        ref = getattr(cmap, name)
        if ref:
            try:
                field_of[resolve_column(ref, headers)] = name
            except LookupError:
                pass
    for ref in cmap.extra_description:
        try:
            field_of.setdefault(resolve_column(ref, headers), "description +")
        except LookupError:
            pass

    rows = stored[:20]
    present = {k for row in rows for k, v in row.txn.raw_row.items() if v.strip()}
    letters = sorted(present | set(headers) | set(field_of), key=lambda c: (len(c), c))
    labels, mapped = [], set()
    for letter in letters:
        label = f"{letter} · {headers.get(letter, '')}".rstrip(" ·")
        if letter in field_of:
            label += f" → {field_of[letter]}"
            mapped.add(label)
        labels.append(label)
    df = pd.DataFrame([[row.txn.raw_row.get(c, "") for c in letters] for row in rows], columns=labels)
    styled = df.style.apply(lambda col: [_MAPPED_BG if col.name in mapped else ""] * len(col), axis=0)
    st.dataframe(styled, hide_index=True, width="stretch")
    st.caption(f"Header row {profile.header_row} · date format `{profile.date_format}` · "
               f"sign: {_sign_convention(rules, headers)} · showing {len(rows)} of {len(stored)} rows")

    if imp["unparsed"]:
        with st.expander(f"{len(imp['unparsed'])} unparsed rows (excluded from totals)"):
            st.dataframe(pd.DataFrame([
                {"Row": u["row_number"], "Reason": u["reason"],
                 "Cells": " | ".join(v for v in u["raw_row"].values() if v.strip())}
                for u in imp["unparsed"]]), hide_index=True, width="stretch")


# ---------------------------------------------------------------- 4. rules preview

def _decided_by(row: StoredTxn, rules_by_id: dict[int, Rule], headers: dict[str, str]) -> str:
    o = row.outcome
    if o.decided_by == "user":
        return "you"
    rule = rules_by_id.get(o.matched_rule_id)
    if rule is not None:
        return describe_condition(rule, headers)
    if o.status == "skipped":
        return "income · not counted"
    if o.status == "needs_sign":
        return "no sign rule matched"
    return "no rule matched"


def _reset_button(conn: sqlite3.Connection, container, row: StoredTxn) -> None:
    if row.outcome.decided_by == "user" and container.button(
            ":material/undo:", key=f"reset_{row.id}", help="Undo your decision"):
        pipeline.reset(conn, row.id)
        st.rerun()


def _result_cell(conn: sqlite3.Connection, cell, row: StoredTxn, categories: list[Category]) -> None:
    o, t = row.outcome, row.txn
    cat_ids = [c.id for c in categories]
    names = {c.id: c.name for c in categories}
    colors = {c.id: c.color for c in categories}

    if o.status in ("categorised", "skipped", "duplicate"):
        a, b = cell.columns([4, 1], vertical_alignment="center")
        if o.status == "categorised":
            a.markdown(pill(names.get(o.category_id, f"#{o.category_id}"), color=colors.get(o.category_id)),
                       unsafe_allow_html=True)
        else:
            a.markdown(pill(o.status.capitalize()), unsafe_allow_html=True)
        _reset_button(conn, b, row)
        return

    if o.status == "needs_sign":
        a, b, c = cell.columns([1, 1, 1.5], vertical_alignment="center")
        save = c.checkbox("Save as sign rule", key=f"signrule_{row.id}")
        for container, label, is_debit in ((a, "Expense", True), (b, "Income", False)):
            if container.button(label, key=f"{'exp' if is_debit else 'inc'}_{row.id}", width="stretch"):
                pipeline.set_sign(conn, row.id, is_debit, save_rule=save)
                st.rerun()
        return

    if o.review_reason:  # flagged by a review rule: Keep (pick a category) or Skip
        cell.markdown(pill(o.review_reason, "amber"), unsafe_allow_html=True)
        a, b = cell.columns([2.2, 1], vertical_alignment="center")
        keep = a.selectbox("Keep as", cat_ids, key=f"keep_{row.id}", format_func=names.get, index=None,
                           placeholder="Keep as…", label_visibility="collapsed")
        if keep is not None:
            pipeline.assign(conn, row.id, keep)
            st.rerun()
        if b.button("Skip", key=f"skip_{row.id}", width="stretch"):
            pipeline.skip(conn, row.id)
            st.rerun()
        return

    a, b = cell.columns([2.2, 1], vertical_alignment="center")
    chosen = a.selectbox("Assign", cat_ids, key=f"assign_{row.id}", format_func=names.get, index=None,
                         placeholder="Assign…", label_visibility="collapsed")
    if chosen is not None:
        pipeline.assign(conn, row.id, chosen)
        st.rerun()
    if b.button("Make rule", key=f"make_{row.id}", width="stretch"):
        rule_form.request_draft(
            origin=f"From row: {t.description} — pick a category, check the value, then Save rule.",
            action="categorise", target="description", operator="contains",
            pattern=suggest_pattern(t.description) or t.description.lower(), priority=20)
        st.rerun()


def _rules_preview(conn: sqlite3.Connection, imp: dict, stored: list[StoredTxn], rules: list[Rule],
                   categories: list[Category], summary: FileSummary) -> None:
    st.markdown("#### Rules preview")
    counts = (f"{summary.matched} matched · {summary.skipped} skipped · "
              f"{summary.needs_category} need a category")
    if summary.pending:
        counts += " " + pill(f"{summary.pending} need a decision", "amber")
    html(counts)
    only = st.toggle("Show only uncategorised", key=f"only_unc_{imp['id']}")
    rows = [r for r in stored if not only or r.outcome.status in ("uncategorised", "needs_sign")]

    widths = [0.8, 2.4, 1, 3.2, 2.1]
    head = st.columns(widths)
    for col, label in zip(head, ["Date", "Description", "Amount", "Result", "Decided by"]):
        col.markdown(f"<span class='muted'>{label}</span>", unsafe_allow_html=True)
    rules_by_id = {r.id: r for r in rules}
    for row in rows:
        t = row.txn
        c = st.columns(widths, vertical_alignment="center")
        c[0].markdown(f"<span class='muted'>{t.txn_date:%d %b}</span>", unsafe_allow_html=True)
        c[1].markdown(escape(t.description), unsafe_allow_html=True)
        income = row.outcome.is_debit is False
        amount = ("+" if income else "") + money(t.amount_cents)
        style = "color:#15803D" if income else ""
        c[2].markdown(f"<div class='amount' style='{style}'>{amount}</div>", unsafe_allow_html=True)
        _result_cell(conn, c[3], row, categories)
        c[4].markdown(f"<span class='muted'>{escape(_decided_by(row, rules_by_id, imp['headers']))}</span>",
                      unsafe_allow_html=True)
    if not rows:
        st.caption("Nothing left to categorise." if only else "No rows.")


# ---------------------------------------------------------------- cards

def render(conn: sqlite3.Connection, *, session_id: int, categories: list[Category]) -> None:
    """All file cards of the session in upload order, then an upload card if no draft exists."""
    imports = repo.list_imports(conn, session_id)
    open_id = active_id(imports)
    draft = draft_import(imports)
    profiles = {p.id: p for p in repo.list_profiles(conn)}
    for imp in imports:
        if imp["id"] == open_id:
            _open_card(conn, session_id, imp, profiles[imp["profile_id"]], categories,
                       with_uploader=draft is not None and imp["id"] == draft["id"])
        else:
            _collapsed_card(imp, profiles[imp["profile_id"]].name, pipeline.file_summary(conn, imp["id"]))
    if draft is None:
        with st.container(border=True):
            _upload(conn, session_id, None)


def _open_card(conn: sqlite3.Connection, session_id: int, imp: dict, profile: Profile,
               categories: list[Category], *, with_uploader: bool) -> None:
    with st.container(border=True):
        file_available = False
        if with_uploader:  # the draft keeps its uploader, for Edit mapping and replacing the file
            show_card, file_available = _upload(conn, session_id, imp)
            if not show_card:
                return
        stored = repo.list_transactions(conn, imp["id"])
        rules = repo.list_rules(conn, profile_id=profile.id)
        summary = pipeline.file_summary(conn, imp["id"])
        _file_line(conn, imp, profile, summary, file_available)

        _preview(imp, profile, stored, rules)

        left, right = st.columns([1, 1.35], gap="large")
        with left:
            st.markdown("#### Rules")
            rule_form.render_form(conn, profile=profile, headers=imp["headers"], stored=stored,
                                  categories=categories, session_id=session_id)
            st.divider()
            rule_form.render_rule_list(conn, profile=profile, headers=imp["headers"], stored=stored,
                                       categories=categories, session_id=session_id)
        with right:
            _rules_preview(conn, imp, stored, rules, categories, summary)

        st.markdown("#### Category result · this file")
        result_tiles(summary)
        if summary.pending:
            st.warning(f"{summary.pending} row(s) need a decision ({money(summary.pending_cents)}); "
                       "they're left out of the total until you decide.")
