"""Shared UI helpers: styles, money formatting, pills and result tiles."""
from __future__ import annotations

from decimal import Decimal
from html import escape

import streamlit as st

from core.summary import UNCATEGORISED, FileSummary

AMBER = "#D97706"

_CSS = """
<style>
.pill {display:inline-block; padding:1px 9px; border-radius:999px; font-size:0.8rem;
       line-height:1.5; white-space:nowrap; border:1px solid transparent;}
.pill-neutral {background:rgba(128,128,128,.14);}
.pill-amber {background:rgba(217,119,6,.15); color:#B45309; border-color:rgba(217,119,6,.35);}
.pill-green {background:rgba(22,163,74,.13); color:#15803D;}
.pill-red {background:rgba(220,38,38,.12); color:#B91C1C;}
.dot {display:inline-block; width:.6rem; height:.6rem; border-radius:50%; margin-right:.35rem;}
.tiles {display:grid; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:.6rem; margin:.4rem 0 1rem;}
.tile {border:1px solid rgba(128,128,128,.25); border-radius:10px; padding:.6rem .8rem;}
.tile .name {font-size:.8rem; opacity:.75;}
.tile .amt {font-size:1.25rem; font-weight:600; font-variant-numeric:tabular-nums;}
.tile-amber {border-color:rgba(217,119,6,.55); background:rgba(217,119,6,.08);}
.tile-total {background:#1F2937; color:#F9FAFB; border-color:#1F2937;}
.muted {opacity:.65; font-size:.85rem;}
.grand-total {font-size:2.4rem; font-weight:700; font-variant-numeric:tabular-nums; margin-top:.6rem;}
.bar-row {display:grid; grid-template-columns:minmax(6rem,11rem) 1fr 6.5rem 3.5rem; gap:.75rem;
          align-items:center; padding:.25rem 0;}
.bar {background:rgba(128,128,128,.12); border-radius:4px; height:.7rem; overflow:hidden;}
.bar > div {height:100%; border-radius:4px;}
.bar-total {border-top:1px solid rgba(128,128,128,.35); margin-top:.3rem; padding-top:.5rem; font-weight:600;}
.amount {font-variant-numeric:tabular-nums; text-align:right;}
</style>
"""


def inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def money(cents: int) -> str:
    """1784 -> '17.84', -120000 -> '-1,200.00'."""
    return f"{Decimal(cents) / 100:,.2f}"


def pill(text: str, kind: str = "neutral", color: str | None = None) -> str:
    """HTML pill; with `color`, a coloured dot precedes the text."""
    dot = f'<span class="dot" style="background:{escape(color)}"></span>' if color else ""
    return f'<span class="pill pill-{kind}">{dot}{escape(text)}</span>'


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def result_tiles(summary: FileSummary, total_label: str = "File total") -> None:
    """One tile per category, Uncategorised in amber, and a dark total tile."""
    tiles = []
    for t in summary.totals:
        kind = " tile-amber" if t.name == UNCATEGORISED else ""
        dot = "" if t.name == UNCATEGORISED else f'<span class="dot" style="background:{escape(t.color)}"></span>'
        tiles.append(f'<div class="tile{kind}"><div class="name">{dot}{escape(t.name)}</div>'
                     f'<div class="amt">{money(t.cents)}</div></div>')
    tiles.append(f'<div class="tile tile-total"><div class="name">{escape(total_label)}</div>'
                 f'<div class="amt">{money(summary.total_cents)}</div></div>')
    html(f'<div class="tiles">{"".join(tiles)}</div>')


def validation_messages(exc: Exception) -> list[str]:
    """Readable messages from a pydantic ValidationError (or any exception)."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        return [e["msg"].removeprefix("Value error, ") for e in errors()]
    return [str(exc)]
