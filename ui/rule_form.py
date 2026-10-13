"""Add/edit rule form and the ordered rule list for the active file.

Form state lives in st.session_state under rf_* keys. To load a rule into the
form (Edit, Make rule), call request_prefill() and st.rerun(): the values are
applied before the widgets are created on the next run.
"""
from __future__ import annotations

import sqlite3
from collections import Counter

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from core import pipeline
from core.columns import resolve_column
from core.models import AMOUNT_OPERATORS, PATTERN_OPERATORS, TEXT_OPERATORS, Category, Profile, Rule, StoredTxn
from core.rules import describe_condition, describe_result, test_rule
from db import repo
from ui.common import html, money, pill, validation_messages

ACTIONS = {"categorise": "Category", "skip": "Skip", "review": "Review", "sign": "Sign"}
ACTION_HEADINGS = {"sign": "Sign rules", "review": "Review rules", "skip": "Skip rules", "categorise": "Category rules"}
_TEXT_OPS = ["contains", "starts_with", "equals", "regex"]
_PLACEHOLDERS = {"amount_between": "e.g. 0,5", "regex": r"e.g. grab\*?food", "equals": "e.g. DR"}

_EMPTY = {
    "rf_id": None, "rf_enabled": True, "rf_action": "categorise", "rf_target": "description",
    "rf_operator": "contains", "rf_pattern": "", "rf_category": None, "rf_note": "",
    "rf_sign": "expense", "rf_scope": "all", "rf_priority": 20, "rf_origin": "",
}


def _form_values(rule: Rule | None, headers: dict[str, str], origin: str = "") -> dict:
    if rule is None:
        return dict(_EMPTY)
    target = rule.target_field
    if target == "column":
        try:
            target = "col:" + resolve_column(rule.column_ref, headers)
        except LookupError:
            target = "col:" + rule.column_ref
    return {
        "rf_id": rule.id, "rf_enabled": rule.enabled, "rf_action": rule.action, "rf_target": target,
        "rf_operator": rule.operator, "rf_pattern": rule.pattern or "", "rf_category": rule.category_id,
        "rf_note": rule.note or "", "rf_sign": rule.sign_result or "expense",
        "rf_scope": "profile" if rule.profile_id is not None else "all",
        "rf_priority": rule.priority, "rf_origin": origin,
    }


def request_prefill(rule: Rule | None, headers: dict[str, str] | None = None, origin: str = "") -> None:
    """Load a rule (or an empty form) on the next run. Call st.rerun() afterwards."""
    st.session_state["rf_pending"] = _form_values(rule, headers or {}, origin)


def request_draft(origin: str = "", **values) -> None:
    """Load a new, possibly incomplete rule (e.g. 'Make rule' before a category is picked)."""
    st.session_state["rf_pending"] = {**_EMPTY, **{f"rf_{k}": v for k, v in values.items()}, "rf_origin": origin}


def _apply_pending() -> None:
    pending = st.session_state.pop("rf_pending", None)
    for key, value in (pending or {}).items():
        st.session_state[key] = value
    for key, value in _EMPTY.items():
        st.session_state.setdefault(key, value)


def _operators_for(target: str) -> list[str]:
    if target == "amount":
        return ["amount_between", "is_negative", "is_positive"]
    if target == "description":
        return list(_TEXT_OPS)
    return [*_TEXT_OPS, "not_empty"]


def _target_label(target: str, headers: dict[str, str]) -> str:
    if target.startswith("col:"):
        letter = target[4:]
        header = headers.get(letter)
        return f"Column {letter} · {header}" if header else f"Column {letter}"
    return target.capitalize()


def build_rule(profile: Profile, *, condition_only: bool = False) -> Rule:
    """Rule from the form state. Raises ValidationError with readable messages.

    condition_only validates just the condition (for 'Test on this file'), so a
    rule can be tested before its category or reason is filled in.
    """
    s = st.session_state
    target = s.rf_target
    target_field, column_ref = ("column", target[4:]) if target.startswith("col:") else (target, None)
    action, operator = ("skip" if condition_only else s.rf_action), s.rf_operator
    data = {
        "id": s.rf_id,
        "enabled": s.rf_enabled,
        "action": action,
        "target_field": target_field,
        "column_ref": column_ref,
        "operator": operator,
        "pattern": (s.rf_pattern.strip() or None) if operator in PATTERN_OPERATORS else None,
        "priority": int(s.rf_priority),
        "profile_id": profile.id if action == "sign" or s.rf_scope == "profile" else None,
    }
    if action == "categorise":
        data["category_id"] = s.rf_category
    if action in ("review", "skip"):
        data["note"] = s.rf_note.strip() or None
    if action == "sign":
        data["sign_result"] = s.rf_sign
    return Rule.model_validate(data)


def render_form(
    conn: sqlite3.Connection, *, profile: Profile, headers: dict[str, str],
    stored: list[StoredTxn], categories: list[Category], session_id: int,
) -> None:
    _apply_pending()
    s = st.session_state
    editing = s.rf_id is not None
    st.markdown(f"**{'Edit rule #' + str(s.rf_id) if editing else 'Add a rule'}**")
    if s.rf_origin:
        st.caption(s.rf_origin)

    letters = sorted({k for row in stored for k in row.txn.raw_row} | set(headers),
                     key=lambda c: (len(c), c))
    targets = ["description", "amount", *(f"col:{c}" for c in letters)]
    if s.rf_target not in targets:
        s.rf_target = "description"

    c1, c2 = st.columns([1.3, 1])
    target = c1.selectbox("Column", targets, key="rf_target", format_func=lambda t: _target_label(t, headers))
    operators = _operators_for(target)
    if s.rf_operator not in operators:
        s.rf_operator = operators[0]
    operator = c2.selectbox("Operator", operators, key="rf_operator", format_func=lambda o: o.replace("_", " "))
    st.text_input("Value", key="rf_pattern", disabled=operator not in PATTERN_OPERATORS,
                  placeholder=_PLACEHOLDERS.get(operator, "e.g. grab" if operator in TEXT_OPERATORS else "—"))

    action = st.radio("Then", list(ACTIONS), key="rf_action", format_func=ACTIONS.get, horizontal=True)
    if action == "categorise":
        ids = [c.id for c in categories]
        if s.rf_category not in ids:
            s.rf_category = None
        names = {c.id: c.name for c in categories}
        st.selectbox("Category", ids, key="rf_category", format_func=names.get,
                     placeholder="Choose a category")
    elif action == "review":
        st.text_input("Reason shown on the row", key="rf_note", placeholder="e.g. Possible refund")
    elif action == "skip":
        st.text_input("Note (optional)", key="rf_note", placeholder="e.g. Own-account transfer")
    else:
        st.radio("Rows matching this are", ["expense", "income"], key="rf_sign", horizontal=True)

    c3, c4 = st.columns([1.3, 1])
    if action == "sign":
        s.rf_scope = "profile"
    c3.radio("Applies to", ["all", "profile"], key="rf_scope", horizontal=True, disabled=action == "sign",
             format_func=lambda v: "All profiles" if v == "all" else profile.name,
             help="Sign rules always belong to one profile." if action == "sign" else None)
    c4.number_input("Priority", min_value=0, step=1, key="rf_priority", help="Lower runs first.")

    b1, b2, b3 = st.columns(3)
    test_clicked = b1.button("Test on this file", key="rf_test", width="stretch")
    save_clicked = b2.button("Save rule", key="rf_save", type="primary", width="stretch")
    if b3.button("Cancel" if editing else "Clear", key="rf_clear", width="stretch"):
        request_prefill(None)
        st.rerun()

    if test_clicked or save_clicked:
        try:
            rule = build_rule(profile, condition_only=test_clicked)
        except ValidationError as exc:
            for msg in validation_messages(exc):
                st.error(msg)
            return
        if test_clicked:
            hits = test_rule(rule, [row.txn for row in stored], headers)
            st.info(f"Matches {len(hits)} of {len(stored)} rows in this file.")
            if hits:
                st.dataframe(pd.DataFrame([
                    {"Date": t.txn_date, "Description": t.description, "Amount": money(t.signed_amount_cents)}
                    for t in hits[:20]]), hide_index=True, width="stretch")
        else:
            pipeline.save_rule(conn, rule, session_id)
            request_prefill(None)
            s["flash"] = f"Rule {'updated' if editing else 'saved'}; results refreshed."
            st.rerun()


def render_rule_list(
    conn: sqlite3.Connection, *, profile: Profile, headers: dict[str, str],
    stored: list[StoredTxn], categories: list[Category], session_id: int,
) -> None:
    rules = repo.list_rules(conn, profile_id=profile.id)
    names = {c.id: c.name for c in categories}
    colors = {c.id: c.color for c in categories}
    decided = Counter(row.outcome.matched_rule_id for row in stored)

    current = None
    for rule in rules:
        if rule.action != current:
            current = rule.action
            st.markdown(f"<div class='muted' style='margin-top:.6rem'>{ACTION_HEADINGS[current]}</div>",
                        unsafe_allow_html=True)
        cols = st.columns([0.45, 3.2, 1.7, 0.6, 0.55, 0.45, 0.45], vertical_alignment="center")
        cols[0].markdown(f"**{rule.priority}**")
        scope = f" <span class='muted'>· {profile.name}</span>" if rule.profile_id is not None else ""
        cols[1].markdown(f"{describe_condition(rule, headers)}{scope}", unsafe_allow_html=True)
        if rule.action == "categorise":
            cols[2].markdown(pill(describe_result(rule, names), color=colors.get(rule.category_id)),
                             unsafe_allow_html=True)
        else:
            kind = {"skip": "neutral", "review": "amber", "sign": "green"}[rule.action]
            cols[2].markdown(pill(describe_result(rule, names), kind), unsafe_allow_html=True)
        cols[3].markdown(f"<span class='muted' title='rows decided by this rule'>{decided.get(rule.id, 0)}</span>",
                         unsafe_allow_html=True)
        enabled = cols[4].toggle("Enabled", value=rule.enabled, key=f"rule_on_{rule.id}",
                                 label_visibility="collapsed")
        if enabled != rule.enabled:
            repo.set_rule_enabled(conn, rule.id, enabled)
            pipeline.rerun_session(conn, session_id)
            st.rerun()
        if cols[5].button(":material/edit:", key=f"rule_edit_{rule.id}", help="Edit"):
            request_prefill(rule, headers)
            st.rerun()
        if cols[6].button(":material/delete:", key=f"rule_del_{rule.id}", help="Delete"):
            repo.delete_rule(conn, rule.id)
            pipeline.rerun_session(conn, session_id)
            s = st.session_state
            if s.get("rf_id") == rule.id:
                request_prefill(None)
            s["flash"] = "Rule deleted; results refreshed."
            st.rerun()
    if not rules:
        st.caption("No rules yet.")
    html("<div class='muted' style='margin-top:.5rem'>Sign rules run first, then review, skip and category "
         "rules. Within each group the lowest number wins. The count is rows decided in this file.</div>")
