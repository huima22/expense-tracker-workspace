"""Profile detection and normalisation: raw frame -> canonical transactions.

Pipeline stages 2 and 3 of DESIGN.md section 4. The normaliser never decides
direction itself; the profile's sign rules set is_debit.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import pandas as pd

from core.columns import col_letter_to_index, index_to_col_letter, resolve_column  # noqa: F401 (re-export)
from core.dedupe import row_hash
from core.models import Profile, Rule, Txn
from core.parsers.base import file_type_of
from core.rules import sign_of


class ProfileError(ValueError):
    """The profile doesn't fit the file (missing header, header row past the end...)."""


@dataclass
class UnparsedRow:
    row_number: int  # 1-based row in the file
    reason: str
    raw_row: dict[str, str]


@dataclass
class NormalisedFile:
    txns: list[Txn] = field(default_factory=list)
    unparsed: list[UnparsedRow] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)  # letter -> header text


# ---------------------------------------------------------------- value parsing

_CURRENCY = re.compile(r"(?i)s\$|sgd|usd|[$€£¥,\s]")
_NUMBER = re.compile(r"\d+(\.\d*)?|\.\d+")


def parse_amount_cents(text: str) -> int:
    """'1,234.50' -> 123450, '(12.00)' -> -1200, '-7.8' -> -780, 'S$ 5' -> 500.

    Strips currency symbols, spaces and thousands commas; rounds to cents half-up.
    """
    s = text.strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    s = _CURRENCY.sub("", s)
    if s.startswith(("-", "+")):
        negative ^= s[0] == "-"
        s = s[1:]
    elif s.endswith("-"):
        negative ^= True
        s = s[:-1]
    if not _NUMBER.fullmatch(s):
        raise ValueError(f"amount {text!r} is not a number")
    try:
        cents = (Decimal(s) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"amount {text!r} is not a number") from exc
    return -int(cents) if negative else int(cents)


def parse_date(text: str, date_format: str) -> date:
    """Parse with the profile's format; ISO dates (from XLSX date cells) are also accepted."""
    s = " ".join(text.split())
    try:
        return datetime.strptime(s, date_format).date()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        raise ValueError(f"date {text!r} doesn't match {date_format!r}") from None


# ---------------------------------------------------------------- detection

def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def header_cells(raw: pd.DataFrame, header_row: int) -> dict[str, str]:
    """Non-empty cells of the header row, letter -> text. Empty if the row doesn't exist."""
    if header_row not in raw.index:
        return {}
    row = raw.loc[header_row]
    return {letter: str(row[letter]).strip() for letter in raw.columns if str(row[letter]).strip()}


def header_matches(raw: pd.DataFrame, profile: Profile) -> bool | None:
    """True/False if the profile has a header_signature, None if it has none to check."""
    if not profile.header_signature:
        return None
    headers = header_cells(raw, profile.header_row)
    return all(_norm(headers.get(letter, "")) == _norm(expected)
               for letter, expected in profile.header_signature.items())


def filename_matches(filename: str, profile: Profile) -> bool:
    """False for profiles without a filename_pattern: they are never auto-detected."""
    return bool(profile.filename_pattern) and re.search(profile.filename_pattern, filename) is not None


@dataclass
class Detection:
    profile: Profile | None = None  # filename and header signature both match
    near_miss: Profile | None = None  # filename matches but the header signature doesn't


def detect(filename: str, profiles: Iterable[Profile], read: Callable[[Profile], pd.DataFrame]) -> Detection:
    """Match on file type and filename_pattern, then confirm the header_signature.

    `read(profile)` returns the raw frame as that profile reads it (its sheet and
    encoding). The first profile whose signature cells sit in its header_row wins;
    a profile without a signature matches on filename alone. A profile that only
    fails the signature check is returned as near_miss, so the Header & columns
    step can open pre-filled with its settings.
    """
    file_type = file_type_of(filename)
    near_miss = None
    for profile in profiles:
        if profile.file_type != file_type or not filename_matches(filename, profile):
            continue
        try:
            raw = read(profile)
        except ValueError:  # ParseError: e.g. the profile's sheet isn't in this workbook
            near_miss = near_miss or profile
            continue
        if header_matches(raw, profile) is False:
            near_miss = near_miss or profile
            continue
        return Detection(profile=profile)
    return Detection(near_miss=near_miss)


def detect_profile(filename: str, raw: pd.DataFrame, profiles: Iterable[Profile]) -> Profile | None:
    """detect() for an already-read frame; None when nothing matches."""
    return detect(filename, profiles, lambda _profile: raw).profile


# ---------------------------------------------------------------- normalise

def _resolve(ref: str | None, headers: dict[str, str]) -> str | None:
    if ref is None:
        return None
    try:
        return resolve_column(ref, headers)
    except LookupError as exc:
        raise ProfileError(f"{exc} in the header row") from None


def _signed_cents(cells: dict[str, str], amount: str | None, debit: str | None, credit: str | None) -> int:
    if amount is not None:
        text = cells.get(amount, "")
        if not text.strip():
            raise ValueError("no amount")
        return parse_amount_cents(text)
    filled = [cells.get(col, "") for col in (debit, credit) if col and cells.get(col, "").strip()]
    if not filled:
        raise ValueError("no amount in the debit or credit column")
    if len(filled) == 2:
        raise ValueError("both debit and credit columns hold a value")
    return parse_amount_cents(filled[0])


@dataclass
class ResolvedColumns:
    """The profile's column_map resolved to letters for one file."""

    date: str
    descriptions: list[str]
    amount: str | None
    debit: str | None
    credit: str | None

    @property
    def amounts(self) -> list[str]:
        return [c for c in (self.amount, self.debit, self.credit) if c]

    def date_text(self, cells: dict[str, str]) -> str:
        return cells.get(self.date, "")

    def signed_cents(self, cells: dict[str, str]) -> int:
        return _signed_cents(cells, self.amount, self.debit, self.credit)

    def description(self, cells: dict[str, str]) -> str:
        return " ".join(" ".join(cells.get(c, "") for c in self.descriptions).split())


def resolve_columns(profile: Profile, headers: dict[str, str]) -> ResolvedColumns:
    cmap = profile.column_map
    return ResolvedColumns(
        date=_resolve(cmap.date, headers),
        descriptions=[_resolve(c, headers) for c in [cmap.description, *cmap.extra_description]],
        amount=_resolve(cmap.amount, headers),
        debit=_resolve(cmap.debit, headers),
        credit=_resolve(cmap.credit, headers),
    )


def data_rows(raw: pd.DataFrame, profile: Profile, cols: ResolvedColumns) -> Iterator[tuple[int, dict[str, str]]]:
    """(row number, cells) for every row below the header and above the footer.

    Skips blank rows: those whose date and amount cells are all empty.
    """
    last_row = len(raw) - profile.skip_footer_rows
    for row_number, row in raw.loc[profile.header_row + 1:last_row].iterrows():
        cells = {letter: str(value) for letter, value in row.items()}
        if cols.date_text(cells).strip() or any(cells.get(c, "").strip() for c in cols.amounts):
            yield int(row_number), cells


def check_header_row(raw: pd.DataFrame, profile: Profile) -> None:
    if profile.header_row > len(raw):
        raise ProfileError(f"header row {profile.header_row} is past the end of the file ({len(raw)} rows)")


def normalise(raw: pd.DataFrame, profile: Profile, sign_rules: Iterable[Rule] = ()) -> NormalisedFile:
    """Apply a profile to a raw frame.

    Drops rows above and including the header row, the last skip_footer_rows rows,
    and rows whose date and amount cells are all empty. Rows whose date or amount
    can't be parsed are returned in `unparsed`, never dropped silently.
    """
    check_header_row(raw, profile)
    headers = header_cells(raw, profile.header_row)
    cols = resolve_columns(profile, headers)
    sign_rules = [r for r in sign_rules if profile.id is None or r.profile_id in (None, profile.id)]

    result = NormalisedFile(headers=headers)
    occurrences: Counter[tuple] = Counter()
    for row_number, cells in data_rows(raw, profile, cols):
        try:
            txn_date = parse_date(cols.date_text(cells), profile.date_format)
            signed = cols.signed_cents(cells)
        except ValueError as exc:
            result.unparsed.append(UnparsedRow(row_number, str(exc), cells))
            continue

        description = cols.description(cells)
        key = (txn_date, signed, description.lower())
        occurrence = occurrences[key]
        occurrences[key] += 1
        txn = Txn(
            txn_date=txn_date,
            description=description,
            signed_amount_cents=signed,
            amount_cents=abs(signed),
            row_hash=row_hash(profile.id, txn_date, signed, description, occurrence),
            raw_row=cells,
        )
        txn.is_debit = sign_of(txn, sign_rules, headers)
        result.txns.append(txn)
    return result
