"""Excel-style column letters and resolving a column reference (letter or header name)."""
from __future__ import annotations

import re

_LETTERS = re.compile(r"^[A-Z]{1,3}$")


def is_col_letter(ref: str) -> bool:
    return bool(_LETTERS.match(ref))


def col_letter_to_index(letter: str) -> int:
    """'A' -> 0, 'L' -> 11, 'Z' -> 25, 'AA' -> 26."""
    if not is_col_letter(letter):
        raise ValueError(f"not a column letter: {letter!r}")
    index = 0
    for ch in letter:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def index_to_col_letter(index: int) -> str:
    """0 -> 'A', 11 -> 'L', 26 -> 'AA'."""
    if index < 0:
        raise ValueError(f"column index must be >= 0, got {index}")
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def resolve_column(ref: str, headers: dict[str, str] | None = None) -> str:
    """Return the column letter for a reference.

    A reference of 1-3 capital letters is a column letter; anything else is a
    header name, matched case-insensitively against `headers` (letter -> header text).
    """
    if is_col_letter(ref):
        return ref
    wanted = _norm(ref)
    for letter, header in (headers or {}).items():
        if _norm(header) == wanted:
            return letter
    raise LookupError(f"no column with header {ref!r}")
