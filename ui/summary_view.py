"""Action bar (+ Add new file, Consolidate N files) and the consolidated result with exports."""
from __future__ import annotations

import re
import sqlite3
from html import escape

import streamlit as st

from core import pipeline
from core.summary import UNCATEGORISED, SessionSummary, export_csv, export_xlsx
from db import repo
from ui import file_card
from ui.common import html, money, pill

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def is_consolidated(session: dict) -> bool:
    return bool(st.session_state.get(f"consolidated_{session['id']}") or session["consolidated_at"])


def action_bar(conn: sqlite3.Connection, session_id: int) -> None:
    imports = repo.list_imports(conn, session_id)
    draft = file_card.draft_import(imports)
    count = len(imports)
    c1, c2, _ = st.columns([1.3, 1.7, 3])
    if c1.button("+ Add new file", key="add_file", width="stretch", disabled=draft is None,
                 help="Finish the current file and add another." if draft else "Upload a file first."):
        pipeline.commit(conn, draft["id"])
        file_card.open_card(None)
        file_card.new_uploader()
        st.session_state["flash"] = f"{draft['filename']} committed. Add the next file below."
        st.rerun()
    label = f"Consolidate {count} file{'s' if count != 1 else ''}"
    if c2.button(label, key="consolidate", type="primary", width="stretch", disabled=count == 0):
        pipeline.consolidate(conn, session_id)
        st.session_state[f"consolidated_{session_id}"] = True
        file_card.open_card(file_card.NO_CARD)
        file_card.new_uploader()
        st.rerun()


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "session"


def _bars(summary: SessionSummary) -> None:
    largest = max((t.cents for t in summary.totals), default=0)
    rows = []
    for t in summary.totals:
        width = 100 * t.cents / largest if largest > 0 and t.cents > 0 else 0
        color = "#D97706" if t.name == UNCATEGORISED else t.color
        rows.append(
            f"<div class='bar-row'><span>{escape(t.name)}</span>"
            f"<div class='bar'><div style='width:{width:.1f}%;background:{escape(color)}'></div></div>"
            f"<span class='amount'>{money(t.cents)}</span>"
            f"<span class='amount muted'>{summary.share(t.cents):.0%}</span></div>")
    rows.append(f"<div class='bar-row bar-total'><span>Total</span><div></div>"
                f"<span class='amount'>{money(summary.total_cents)}</span><span></span></div>")
    html("".join(rows))


def render(conn: sqlite3.Connection, session: dict) -> None:
    summary = pipeline.session_summary(conn, session["id"])
    with st.container(border=True):
        st.markdown(f"### Consolidated · {escape(session['name'])}")
        for f in summary.files:
            s = f.summary
            parts = [f"**{escape(f.filename)}**", escape(f.profile), f"{s.rows} rows", f"{s.skipped} skipped"]
            if s.duplicates:
                parts.append(f"{s.duplicates} duplicates")
            parts.append(money(s.total_cents))
            draft = " " + pill("draft") if f.status == "draft" else ""
            st.markdown(" · ".join(parts) + draft, unsafe_allow_html=True)

        files = len(summary.files)
        html(f"<div class='grand-total'>{money(summary.total_cents)}</div>"
             f"<div class='muted'>Total expense across {files} file{'s' if files != 1 else ''}</div>")
        if summary.needs_category:
            st.warning(f"{summary.needs_category} expense row(s) totalling {money(summary.needs_category_cents)} "
                       "are still uncategorised; they're counted under Uncategorised.")
        if summary.pending:
            st.warning(f"{summary.pending} row(s) totalling {money(summary.pending_cents)} still need a decision "
                       "(possible refunds or rows with no direction); they're left out of the total.")

        if summary.totals:
            _bars(summary)
        else:
            st.caption("Nothing counted yet.")

        names = {c.id: c.name for c in repo.list_categories(conn)}
        slug = _slug(session["name"])
        c1, c2, _ = st.columns([1, 1, 3])
        c1.download_button(
            "Export XLSX", key="export_xlsx", width="stretch", mime=XLSX_MIME, file_name=f"{slug}-expenses.xlsx",
            data=export_xlsx(summary, pipeline.session_transactions(conn, session["id"]), names, session["name"]))
        c2.download_button("Export CSV", key="export_csv", width="stretch", mime="text/csv",
                           file_name=f"{slug}-expenses.csv", data=export_csv(summary))
