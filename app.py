"""Local Expense Tracker — single Streamlit page.

    streamlit run app.py

Header -> categories bar -> one card per file -> action bar -> consolidated result.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date

import streamlit as st
from pydantic import ValidationError

from core import pipeline
from core.models import Category
from db import repo
from db.connection import get_conn
from ui import file_card, summary_view
from ui.common import html, inject_css, pill, validation_messages


def current_session(conn: sqlite3.Connection) -> int:
    """The session in use: remembered, else the newest, else a new '<Month YYYY>' session."""
    s = st.session_state
    if s.get("session_id") and repo.get_session(conn, s["session_id"]):
        return s["session_id"]
    sessions = repo.list_sessions(conn)
    s["session_id"] = sessions[0]["id"] if sessions else repo.create_session(conn, f"{date.today():%B %Y}")
    return s["session_id"]


def header(conn: sqlite3.Connection, session_id: int) -> None:
    session = repo.get_session(conn, session_id)
    imports = repo.list_imports(conn, session_id)
    open_rows = 0
    for imp in imports:
        summary = pipeline.file_summary(conn, imp["id"])
        open_rows += summary.needs_category + summary.pending

    c1, c2, c3 = st.columns([2.2, 2, 2], vertical_alignment="bottom")
    c1.title("Expense tracker")
    name = c2.text_input("Session", value=session["name"], key=f"session_name_{session_id}")
    if name.strip() and name.strip() != session["name"]:
        repo.rename_session(conn, session_id, name)
    pills = pill(f"{len(imports)} file{'s' if len(imports) != 1 else ''} loaded")
    pills += " " + pill(f"{open_rows} uncategorised", "amber" if open_rows else "green")
    with c3:
        html(f"<div style='padding-bottom:.6rem'>{pills}</div>")


def categories_bar(conn: sqlite3.Connection, categories: list[Category]) -> None:
    c1, c2 = st.columns([6, 1], vertical_alignment="center")
    chips = " ".join(pill(c.name, color=c.color) for c in categories) or "<span class='muted'>No categories</span>"
    with c1:
        html(chips)
    with c2.popover("+ New category", width="stretch"):
        name = st.text_input("Name", key="new_cat_name")
        color = st.color_picker("Colour", value="#9AA3AE", key="new_cat_color")
        if st.button("Add category", key="new_cat_add", type="primary"):
            try:
                order = max((c.sort_order for c in repo.list_categories(conn)), default=0) + 10
                repo.create_category(conn, Category(name=name, color=color, sort_order=order))
            except ValidationError as exc:
                st.error("; ".join(validation_messages(exc)))
            except sqlite3.IntegrityError:
                st.error(f"A category named '{name.strip()}' already exists.")
            else:
                st.session_state["flash"] = f"Category '{name.strip()}' added."
                st.rerun()


def main(conn: sqlite3.Connection) -> None:
    session_id = current_session(conn)
    categories = repo.list_categories(conn, active_only=True)
    header(conn, session_id)
    categories_bar(conn, categories)
    if flash := st.session_state.pop("flash", None):
        st.toast(flash)
    file_card.render(conn, session_id=session_id, categories=categories)
    summary_view.action_bar(conn, session_id)
    session = repo.get_session(conn, session_id)
    if summary_view.is_consolidated(session):
        summary_view.render(conn, session)


st.set_page_config(page_title="Expense tracker", layout="wide")
inject_css()
with closing(get_conn()) as connection:
    main(connection)
