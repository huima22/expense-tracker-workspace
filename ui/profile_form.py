"""Header & columns step: point at the header row, map the columns, check live, save as a profile.

Shown in the file card when no profile matches an upload, when the filename
matches a profile but its header doesn't (pre-filled from that profile), or
after "Edit mapping". Form state lives under hc_* keys and is re-initialised
whenever a different file or base profile opens the step.
"""
from __future__ import annotations

import sqlite3
from datetime import date

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from core import pipeline
from core.mapping import (
    DATE_FORMATS, DRAFT_PROFILE_ID, LAYOUTS, Mapping, build_profile, check, filename_pattern_for,
    guess_columns, guess_date_format, guess_profile_name, mapping_from_profile, sign_rules_for, suggest_skip,
)
from core.models import Profile
from core.parsers import ParseError, detect_encoding, read_raw, sheet_names
from core.profiles import ProfileError, header_cells
from db import repo
from ui import rule_form
from ui.common import money, validation_messages

CUSTOM = "custom"
PREVIEW_ROWS = 25
_HEADER_STYLE = "background-color: rgba(59,130,246,.22); font-weight: 600"
_SKIPPED_STYLE = "color: rgba(128,128,128,.75)"
_FOOTER_STYLE = "color: rgba(128,128,128,.75); text-decoration: line-through"
_COLUMN_KEYS = ("hc_date", "hc_desc", "hc_amount", "hc_debit", "hc_credit", "hc_indicator")


# ---------------------------------------------------------------- state

def _initial_values(conn: sqlite3.Connection, filename: str, data: bytes, file_type: str,
                    base: Profile | None) -> dict:
    sheets = sheet_names(data, file_type)
    if base is not None:
        sheet = base.sheet_name if base.sheet_name in sheets else (sheets[0] if sheets else None)
        raw = read_raw(data, file_type, sheet_name=sheet)
        sign_rules = repo.list_rules(conn, action="sign", profile_id=base.id)
        m = mapping_from_profile(base, sign_rules, header_cells(raw, base.header_row))
        m.sheet_name = sheet
        name, pattern = base.name, base.filename_pattern or filename_pattern_for(filename, file_type)
    else:
        sheet = sheets[0] if sheets else None
        raw = read_raw(data, file_type, sheet_name=sheet)
        m = Mapping(skip_rows=suggest_skip(raw) or 0, sheet_name=sheet)
        guesses = guess_columns(header_cells(raw, m.header_row))
        for field_name, letter in guesses.items():
            setattr(m, field_name, letter)
        m.layout = "debit_credit" if {"debit_col", "credit_col"} <= guesses.keys() else "positive_spend"
        m.date_format = guess_date_format(raw, m.date_col, m.header_row) or m.date_format
        name, pattern = guess_profile_name(filename, file_type), filename_pattern_for(filename, file_type)

    custom = m.date_format not in DATE_FORMATS
    return {
        "hc_sheet": sheet, "hc_skip": m.skip_rows, "hc_footer": m.footer_rows,
        "hc_suggested": suggest_skip(raw),
        "hc_date": m.date_col, "hc_fmt": CUSTOM if custom else m.date_format,
        "hc_fmt_custom": m.date_format if custom else "",
        "hc_desc": m.description_col, "hc_extra": list(m.extra_description),
        "hc_layout": m.layout, "hc_amount": m.amount_col, "hc_debit": m.debit_col, "hc_credit": m.credit_col,
        "hc_indicator": m.indicator_col, "hc_dr": m.debit_value, "hc_cr": m.credit_value,
        "hc_name": name, "hc_pattern": pattern, "hc_as_new": False,
    }


def reset() -> None:
    """Forget the step's state (after saving or cancelling)."""
    for key in [k for k in st.session_state if str(k).startswith("hc_")]:
        del st.session_state[key]


# Streamlit drops the state of widgets that weren't drawn in a run (e.g. the DR/CR
# fields while another amount layout is chosen); these defaults bring them back.
_DEFAULTS = {"hc_fmt_custom": "", "hc_extra": [], "hc_dr": "DR", "hc_cr": "CR", "hc_as_new": False}


def _ensure_defaults() -> None:
    for key, value in _DEFAULTS.items():
        st.session_state.setdefault(key, value)


def _mapping_from_state(has_sheets: bool) -> Mapping:
    s = st.session_state
    return Mapping(
        skip_rows=int(s.hc_skip),
        footer_rows=int(s.hc_footer),
        sheet_name=s.get("hc_sheet") if has_sheets else None,
        date_col=s.get("hc_date"),
        date_format=(s.hc_fmt_custom.strip() if s.hc_fmt == CUSTOM else s.hc_fmt),
        description_col=s.get("hc_desc"),
        extra_description=list(s.hc_extra),
        layout=s.hc_layout,
        amount_col=s.get("hc_amount"),
        debit_col=s.get("hc_debit"),
        credit_col=s.get("hc_credit"),
        indicator_col=s.get("hc_indicator"),
        debit_value=s.hc_dr,
        credit_value=s.hc_cr,
    )


# ---------------------------------------------------------------- pieces

def _raw_table(rows: pd.DataFrame, header_row: int, footer_start: int) -> None:
    def style(row: pd.Series) -> list[str]:
        if row.name == header_row:
            css = _HEADER_STYLE
        elif row.name < header_row:
            css = _SKIPPED_STYLE
        elif row.name >= footer_start:
            css = _FOOTER_STYLE
        else:
            css = ""
        return [css] * len(row)

    st.dataframe(rows.style.apply(style, axis=1), width="stretch")


def _live_check(result, has_rows: bool) -> None:
    st.markdown("**Live check**")
    if result.missing:
        st.info(f"Map the {', '.join(result.missing)} to see the live check.")
        return
    if result.error:
        st.error(result.error)
        return
    if not result.rows:
        st.warning("No data rows between the header and the footer.")
        return
    if result.ok:
        st.success(result.summary())
    else:
        st.warning(f"{result.summary()} — at least 90% of rows must parse to save "
                   f"({result.parsed_share:.0%} do now).")
    direction = {True: "Expense", False: "Income", None: "—"}
    st.dataframe(pd.DataFrame([
        {"Date": t.txn_date, "Description": t.description, "Amount": money(t.signed_amount_cents),
         "Type": direction[t.is_debit]}
        for t in result.txns[:10]]), hide_index=True, width="stretch")
    if result.unparsed:
        with st.expander(f"{len(result.unparsed)} rows that don't parse"):
            st.dataframe(pd.DataFrame([
                {"Row": u.row_number, "Reason": u.reason,
                 "Cells": " | ".join(v for v in u.raw_row.values() if v.strip())}
                for u in result.unparsed]), hide_index=True, width="stretch")


# ---------------------------------------------------------------- step

def render(
    conn: sqlite3.Connection, *, session_id: int, filename: str, data: bytes, file_type: str, sha256: str,
    base: Profile | None = None, editing: bool = False, replace_import_id: int | None = None,
) -> None:
    """Render the step for one uploaded file. `base` pre-fills from an existing profile;
    `editing` means the user chose Edit mapping (saving updates `base`)."""
    s = st.session_state
    token = f"{sha256}:{base.id if base else 'new'}"
    if s.get("hc_for") != token:
        reset()
        for key, value in _initial_values(conn, filename, data, file_type, base).items():
            s[key] = value
        s["hc_for"] = token
    _ensure_defaults()

    st.markdown("#### Header & columns")
    if editing:
        st.caption(f"Editing profile '{base.name}'. Saving re-imports this file with the new settings; "
                   "decisions you made on its rows are cleared.")
    elif base is not None:
        st.info(f"The file name matches the profile '{base.name}', but its header row doesn't. "
                "The settings below start from that profile — adjust them and save.")
    else:
        st.info("No saved profile matches this file. Show where the header is and which columns hold what. "
                "It's saved as a profile, so this bank's files import automatically next time.")

    sheets = sheet_names(data, file_type)
    if sheets:
        st.selectbox("Sheet", sheets, key="hc_sheet")
    raw = read_raw(data, file_type, sheet_name=s.hc_sheet if sheets else None)

    c1, c2 = st.columns(2)
    suggested = s.get("hc_suggested")
    skip_label = "Rows to skip before the header" + (f" (suggested: {suggested})" if suggested is not None else "")
    c1.number_input(skip_label, min_value=0, step=1, key="hc_skip")
    c2.number_input("Rows to skip at the end (footer)", min_value=0, step=1, key="hc_footer")
    header_row = int(s.hc_skip) + 1
    footer_start = len(raw) - int(s.hc_footer) + 1

    _raw_table(raw.head(PREVIEW_ROWS), header_row, footer_start)
    st.caption(f"Header row: {header_row} (highlighted) · skipped rows greyed out · "
               f"showing {min(len(raw), PREVIEW_ROWS)} of {len(raw)} rows")
    if len(raw) > PREVIEW_ROWS or s.hc_footer:
        with st.expander("Last rows of the file (footer rows are struck through)"):
            _raw_table(raw.tail(8), header_row, footer_start)

    headers = header_cells(raw, header_row)
    letters = list(raw.columns)
    for key in _COLUMN_KEYS:
        if s.get(key) not in letters:
            s[key] = None
    s.hc_extra = [c for c in s.hc_extra if c in letters]

    def label(letter: str) -> str:
        return f"{letter} · {headers[letter]}" if headers.get(letter) else letter

    st.markdown("**Columns**")
    a, b = st.columns(2)
    a.selectbox("Date column", letters, key="hc_date", format_func=label, placeholder="Choose a column")
    sample = date(2026, 10, 3)
    b.selectbox("Date format", [*DATE_FORMATS, CUSTOM], key="hc_fmt",
                format_func=lambda f: "Custom…" if f == CUSTOM else f"{f}   e.g. {sample.strftime(f)}")
    if s.hc_fmt == CUSTOM:
        b.text_input("Custom date format (strptime)", key="hc_fmt_custom", placeholder="e.g. %d.%m.%Y")
    a.selectbox("Description column", letters, key="hc_desc", format_func=label, placeholder="Choose a column")
    b.multiselect("Extra description columns", letters, key="hc_extra", format_func=label,
                  help="Their text is appended to the description.")

    layout = st.radio("Amount layout", list(LAYOUTS), key="hc_layout", format_func=LAYOUTS.get)
    a, b = st.columns(2)
    if layout == "debit_credit":
        a.selectbox("Debit column (spending)", letters, key="hc_debit", format_func=label, placeholder="Choose")
        b.selectbox("Credit column (money in)", letters, key="hc_credit", format_func=label, placeholder="Choose")
    else:
        a.selectbox("Amount column", letters, key="hc_amount", format_func=label, placeholder="Choose a column")
        if layout == "indicator":
            b.selectbox("Indicator column", letters, key="hc_indicator", format_func=label, placeholder="Choose")
            x, y = st.columns(2)
            x.text_input("Value meaning spending", key="hc_dr")
            y.text_input("Value meaning money in", key="hc_cr")

    mapping = _mapping_from_state(bool(sheets))
    result = check(raw, mapping, file_type)
    _live_check(result, bool(len(raw)))

    st.markdown("**Save as profile**")
    a, b = st.columns(2)
    a.text_input("Profile name", key="hc_name")
    b.text_input("File name pattern (regex)", key="hc_pattern",
                 help="Files whose name matches this, and whose header row matches, use this profile automatically.")
    if base is not None and not editing:
        st.checkbox(f"Save as a new profile instead of updating '{base.name}'", key="hc_as_new")

    a, b = st.columns([1, 1])
    save = a.button("Save as profile", key="hc_save", type="primary", disabled=not result.ok, width="stretch")
    if editing and b.button("Cancel", key="hc_cancel", width="stretch"):
        s.pop("hc_edit", None)
        reset()
        st.rerun()
    if not save:
        return

    profile_id = base.id if base is not None and not s.hc_as_new else None
    encoding = detect_encoding(data, base.encoding if base else None) if file_type == "csv" else "utf-8-sig"
    try:
        profile = build_profile(mapping, name=s.hc_name.strip(), file_type=file_type, headers=headers,
                                filename_pattern=s.hc_pattern.strip(), encoding=encoding, profile_id=profile_id)
    except ValidationError as exc:
        for msg in validation_messages(exc):
            st.error(msg)
        return
    try:
        _, import_id = pipeline.save_profile_and_import(
            conn, session_id=session_id, filename=filename, data=data, profile=profile,
            sign_rules=sign_rules_for(mapping, profile_id or DRAFT_PROFILE_ID),
            replace_import_id=replace_import_id)
    except sqlite3.IntegrityError:
        st.error(f"A profile named '{profile.name}' already exists. Choose another name.")
        return
    except (ParseError, ProfileError) as exc:
        st.error(f"Saved the profile, but importing the file failed: {exc}")
        return
    s.pop("hc_edit", None)
    reset()
    s["active_import_id"] = import_id
    s["flash"] = f"Profile '{profile.name}' saved; {filename} imported."
    rule_form.request_prefill(None)
    st.rerun()
