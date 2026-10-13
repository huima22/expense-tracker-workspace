"""Header & columns step: turn the user's mapping choices into a profile and sign rules.

Pure: no Streamlit, no database. The UI collects a Mapping, calls check() for the
live preview, and build_profile() / sign_rules_for() when saving. Columns are
always stored by letter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from core.columns import resolve_column
from core.models import Profile, Rule, Txn
from core.profiles import (
    ProfileError, UnparsedRow, check_header_row, data_rows, header_cells, normalise, parse_date, resolve_columns,
)

LAYOUTS = {
    "positive_spend": "One amount column, spending is positive",
    "negative_spend": "One amount column, spending is negative",
    "debit_credit": "Separate debit and credit columns",
    "indicator": "One amount column plus a DR/CR indicator column",
}
DATE_FORMATS = ["%d %b %Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"]
MIN_PARSED_SHARE = 0.9
DRAFT_PROFILE_ID = 0  # sign rules need a profile id before the profile is saved


@dataclass
class Mapping:
    """Everything the Header & columns step asks for. Column fields hold letters."""

    skip_rows: int = 0
    footer_rows: int = 0
    sheet_name: str | None = None
    date_col: str | None = None
    date_format: str = "%d %b %Y"
    description_col: str | None = None
    extra_description: list[str] = field(default_factory=list)
    layout: str = "debit_credit"
    amount_col: str | None = None
    debit_col: str | None = None
    credit_col: str | None = None
    indicator_col: str | None = None
    debit_value: str = "DR"
    credit_value: str = "CR"

    @property
    def header_row(self) -> int:
        return self.skip_rows + 1

    def missing(self) -> list[str]:
        """Required fields still empty, in the order the form shows them."""
        need = []
        if not self.date_col:
            need.append("date column")
        if not self.date_format.strip():
            need.append("date format")
        if not self.description_col:
            need.append("description column")
        if self.layout == "debit_credit":
            if not self.debit_col:
                need.append("debit column")
            if not self.credit_col:
                need.append("credit column")
        else:
            if not self.amount_col:
                need.append("amount column")
            if self.layout == "indicator":
                if not self.indicator_col:
                    need.append("indicator column")
                if not (self.debit_value.strip() and self.credit_value.strip()):
                    need.append("indicator values")
        return need

    def column_map(self) -> dict:
        cmap = {"date": self.date_col, "description": self.description_col,
                "extra_description": [c for c in self.extra_description if c != self.description_col]}
        if self.layout == "debit_credit":
            cmap |= {"debit": self.debit_col, "credit": self.credit_col}
        else:
            cmap["amount"] = self.amount_col
        return cmap

    def mapped_columns(self) -> list[str]:
        cols = [self.date_col, self.description_col, *self.extra_description]
        cols += [self.debit_col, self.credit_col] if self.layout == "debit_credit" else [self.amount_col]
        if self.layout == "indicator":
            cols.append(self.indicator_col)
        return list(dict.fromkeys(c for c in cols if c))


# ---------------------------------------------------------------- profile and sign rules

def build_profile(
    mapping: Mapping, *, name: str, file_type: str, headers: dict[str, str],
    filename_pattern: str | None = None, encoding: str = "utf-8-sig", profile_id: int | None = None,
) -> Profile:
    """Profile from a complete mapping. header_signature = header text of the mapped columns."""
    signature = {c: headers[c] for c in mapping.mapped_columns() if headers.get(c, "").strip()}
    return Profile.model_validate({
        "id": profile_id,
        "name": name,
        "file_type": file_type,
        "filename_pattern": filename_pattern or None,
        "header_row": mapping.header_row,
        "header_signature": signature or None,
        "skip_footer_rows": mapping.footer_rows,
        "sheet_name": mapping.sheet_name,
        "column_map": mapping.column_map(),
        "date_format": mapping.date_format,
        "encoding": encoding,
    })


def sign_rules_for(mapping: Mapping, profile_id: int) -> list[Rule]:
    """The expense and income sign rules for the amount layout (DESIGN.md section 7 table)."""
    def rule(priority: int, result: str, **condition) -> Rule:
        return Rule(action="sign", sign_result=result, profile_id=profile_id, priority=priority, **condition)

    amount = {"target_field": "amount"}
    if mapping.layout == "positive_spend":
        return [rule(1, "expense", operator="is_positive", **amount),
                rule(2, "income", operator="is_negative", **amount)]
    if mapping.layout == "negative_spend":
        return [rule(1, "expense", operator="is_negative", **amount),
                rule(2, "income", operator="is_positive", **amount)]
    if mapping.layout == "debit_credit":
        return [rule(1, "expense", target_field="column", column_ref=mapping.debit_col, operator="not_empty"),
                rule(2, "income", target_field="column", column_ref=mapping.credit_col, operator="not_empty")]
    if mapping.layout == "indicator":
        column = {"target_field": "column", "column_ref": mapping.indicator_col, "operator": "equals"}
        return [rule(1, "expense", pattern=mapping.debit_value.strip(), **column),
                rule(2, "income", pattern=mapping.credit_value.strip(), **column)]
    raise ValueError(f"unknown amount layout {mapping.layout!r}")


def mapping_from_profile(profile: Profile, sign_rules: list[Rule], headers: dict[str, str]) -> Mapping:
    """Turn a saved profile (and its sign rules) back into form values, to edit it."""
    def letter(ref: str | None) -> str | None:
        if ref is None:
            return None
        try:
            return resolve_column(ref, headers)
        except LookupError:
            return None

    cmap = profile.column_map
    m = Mapping(
        skip_rows=profile.header_row - 1,
        footer_rows=profile.skip_footer_rows,
        sheet_name=profile.sheet_name,
        date_col=letter(cmap.date),
        date_format=profile.date_format,
        description_col=letter(cmap.description),
        extra_description=[c for c in (letter(r) for r in cmap.extra_description) if c],
    )
    signs = sorted((r for r in sign_rules if r.action == "sign"), key=lambda r: (r.priority, r.id or 0))
    expense = next((r for r in signs if r.sign_result == "expense"), None)
    income = next((r for r in signs if r.sign_result == "income"), None)
    if cmap.debit or cmap.credit:
        m.layout, m.debit_col, m.credit_col = "debit_credit", letter(cmap.debit), letter(cmap.credit)
        return m
    m.amount_col = letter(cmap.amount)
    if expense is not None and expense.operator == "equals" and expense.target_field == "column":
        m.layout, m.indicator_col = "indicator", letter(expense.column_ref)
        m.debit_value = expense.pattern or "DR"
        m.credit_value = income.pattern if income is not None and income.pattern else "CR"
    elif expense is not None and expense.operator == "is_negative":
        m.layout = "negative_spend"
    else:
        m.layout = "positive_spend"
    return m


# ---------------------------------------------------------------- live check

@dataclass
class CheckResult:
    missing: list[str] = field(default_factory=list)
    error: str | None = None
    rows: int = 0  # data rows between header and footer
    dates_ok: int = 0
    amounts_ok: int = 0
    expense: int = 0
    income: int = 0
    no_direction: int = 0
    txns: list[Txn] = field(default_factory=list)
    unparsed: list[UnparsedRow] = field(default_factory=list)

    @property
    def parsed_share(self) -> float:
        return len(self.txns) / self.rows if self.rows else 0.0

    @property
    def ok(self) -> bool:
        """Save is allowed: everything mapped and at least 90% of rows parse."""
        return not self.missing and self.error is None and self.rows > 0 and self.parsed_share >= MIN_PARSED_SHARE

    def summary(self) -> str:
        text = (f"Dates parsed: {self.dates_ok}/{self.rows} · Amounts parsed: {self.amounts_ok}/{self.rows} · "
                f"Expense rows: {self.expense} · Income rows: {self.income}")
        if self.no_direction:
            text += f" · No direction: {self.no_direction}"
        return text


def check(raw: pd.DataFrame, mapping: Mapping, file_type: str) -> CheckResult:
    """Normalise with a draft profile and count what parses, for the live check."""
    result = CheckResult(missing=mapping.missing())
    if result.missing:
        return result
    try:
        profile = build_profile(mapping, name="draft", file_type=file_type, headers={})
        check_header_row(raw, profile)
    except (ValueError, ProfileError) as exc:
        result.error = str(exc).splitlines()[0]
        return result

    cols = resolve_columns(profile, header_cells(raw, profile.header_row))
    for _, cells in data_rows(raw, profile, cols):
        result.rows += 1
        try:
            parse_date(cols.date_text(cells), profile.date_format)
            result.dates_ok += 1
        except ValueError:
            pass
        try:
            cols.signed_cents(cells)
            result.amounts_ok += 1
        except ValueError:
            pass

    normalised = normalise(raw, profile, sign_rules_for(mapping, DRAFT_PROFILE_ID))
    result.txns, result.unparsed = normalised.txns, normalised.unparsed
    result.expense = sum(t.is_debit is True for t in result.txns)
    result.income = sum(t.is_debit is False for t in result.txns)
    result.no_direction = sum(t.is_debit is None for t in result.txns)
    return result


# ---------------------------------------------------------------- suggestions

_HEADER_WORDS = re.compile(
    r"(?i)\b(date|description|details|narrative|particulars|amount|debit|credit|withdrawal|deposit|"
    r"balance|transaction|reference|merchant)\b")
_DATE_LIKE = re.compile(r"^\s*(\d{1,4}[/\-. ]([A-Za-z]{3,9}|\d{1,2})[/\-. ]\d{2,4})(\s.*)?$")


def _non_empty(row: pd.Series) -> list[str]:
    return [str(v).strip() for v in row if str(v).strip()]


def suggest_skip(raw: pd.DataFrame, limit: int = 50) -> int | None:
    """Rows to skip before the header: the first row with 3+ filled cells and a header word.

    If no row has header words, the row just above the first date-like data row.
    None when nothing looks like a header or data.
    """
    first_date_row = None
    for row_number, row in raw.head(limit).iterrows():
        cells = _non_empty(row)
        if len(cells) < 3:
            continue
        if any(_HEADER_WORDS.search(c) for c in cells):
            return int(row_number) - 1
        if first_date_row is None and any(_DATE_LIKE.match(c) for c in cells):
            first_date_row = int(row_number)
    if first_date_row is not None:
        return max(first_date_row - 2, 0)
    return None


_GUESSES = [
    ("date_col", r"date"),
    ("description_col", r"description|details|narrative|particulars|merchant|transaction ref1"),
    ("debit_col", r"debit|withdrawal"),
    ("credit_col", r"credit|deposit"),
    ("amount_col", r"amount"),
]


def guess_columns(headers: dict[str, str]) -> dict[str, str]:
    """Pre-fill suggestions from header words: {'date_col': 'A', 'debit_col': 'C', ...}."""
    found: dict[str, str] = {}
    for field_name, pattern in _GUESSES:
        regex = re.compile(rf"(?i)\b({pattern})\b")
        for letter, text in headers.items():
            if regex.search(text) and letter not in found.values():
                found[field_name] = letter
                break
    # 'Debit Amount' also contains 'amount'; one amount column only makes sense without debit/credit
    if "debit_col" in found and "credit_col" in found:
        found.pop("amount_col", None)
    return found


def guess_date_format(raw: pd.DataFrame, date_col: str | None, header_row: int) -> str | None:
    """The common format that parses the most sample cells below the header."""
    if not date_col or date_col not in raw.columns:
        return None
    samples = [v.strip() for v in raw.loc[header_row + 1:, date_col].head(30) if str(v).strip()]
    if not samples:
        return None
    best, best_hits = None, 0
    for fmt in DATE_FORMATS:
        hits = 0
        for value in samples:
            try:
                datetime.strptime(" ".join(value.split()), fmt)
                hits += 1
            except ValueError:
                pass
        if hits > best_hits:
            best, best_hits = fmt, hits
    return best


def _leading_word(filename: str) -> str | None:
    match = re.match(r"[A-Za-z]+", Path(filename).stem)
    return match.group(0) if match else None


_ACRONYMS = {"ocbc", "hsbc", "posb", "amex", "icbc", "rhb", "sc"}


def guess_profile_name(filename: str, file_type: str) -> str:
    """'dbs_oct2026.csv' -> 'DBS CSV'; 'citibank-statement.xlsx' -> 'Citibank XLSX'."""
    word = _leading_word(filename)
    if not word:
        bank = "New bank"
    elif len(word) <= 3 or word.lower() in _ACRONYMS:
        bank = word.upper()
    else:
        bank = word.capitalize()
    return f"{bank} {file_type.upper()}"


def filename_pattern_for(filename: str, file_type: str) -> str:
    """Case-insensitive regex from the file name's leading word: 'DBS_Oct.csv' -> '(?i)^dbs.*\\.csv$'."""
    word = _leading_word(filename)
    prefix = f"^{re.escape(word.lower())}.*" if word else ""
    return rf"(?i){prefix}\.{re.escape(file_type.lower())}$"
