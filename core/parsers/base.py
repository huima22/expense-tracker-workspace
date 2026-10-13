"""Parser protocol, registry and the raw-frame helpers shared by all parsers.

A parser turns file bytes into a raw DataFrame with no header: every cell a
string ('' when empty), columns named by Excel letter (A, B, C ... Z, AA ...),
index = 1-based row number in the file. Only parsers know the file format;
everything downstream works on this frame.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

import pandas as pd

from core.columns import index_to_col_letter
from core.models import Profile


class ParseError(ValueError):
    """The file can't be read as the expected format."""


class Parser(Protocol):
    extensions: tuple[str, ...]  # lower-case, with dot: ('.csv',)

    def read(self, data: bytes, *, sheet_name: str | None = None, encoding: str | None = None) -> pd.DataFrame:
        """Every row of the file (or of the sheet; first sheet when None) as a raw frame."""
        ...

    def sheet_names(self, data: bytes) -> list[str]:
        """Sheet names for workbook formats; [] for single-table formats."""
        ...


_REGISTRY: dict[str, Parser] = {}


def register(parser: Parser) -> None:
    for ext in parser.extensions:
        _REGISTRY[ext] = parser


def file_type_of(filename: str) -> str:
    """'Oct.CSV' -> 'csv'."""
    return Path(filename).suffix.lower().lstrip(".")


def _parser_for_ext(ext: str) -> Parser:
    try:
        return _REGISTRY[ext]
    except KeyError:
        supported = ", ".join(sorted(_REGISTRY)) or "none"
        raise ParseError(f"unsupported file type {ext or '(none)'!r}; supported: {supported}") from None


def get_parser(filename: str) -> Parser:
    return _parser_for_ext(Path(filename).suffix.lower())


def read_raw(data: bytes, file_type: str, *, sheet_name: str | None = None,
             encoding: str | None = None) -> pd.DataFrame:
    """The whole file as a raw frame. file_type is 'csv' or 'xlsx'."""
    return _parser_for_ext(f".{file_type.lower()}").read(data, sheet_name=sheet_name, encoding=encoding)


def raw_preview(data: bytes, file_type: str, n: int = 25, *, sheet_name: str | None = None) -> pd.DataFrame:
    """The first n raw rows, for the Header & columns step."""
    return read_raw(data, file_type, sheet_name=sheet_name).head(n)


def sheet_names(data: bytes, file_type: str) -> list[str]:
    return _parser_for_ext(f".{file_type.lower()}").sheet_names(data)


def parse_file(filename: str, data: bytes, profile: Profile | None = None) -> pd.DataFrame:
    """Read a file with the profile's sheet and encoding (defaults without a profile)."""
    return get_parser(filename).read(
        data,
        sheet_name=profile.sheet_name if profile else None,
        encoding=profile.encoding if profile else None,
    )


def rows_to_frame(rows: list[list[str]]) -> pd.DataFrame:
    """Build the raw frame: every row padded with '' to the widest row's length.

    Trailing rows that are entirely empty (blank lines at the end of a CSV, unused
    spreadsheet rows) are dropped, so footer rows are counted from the last row
    that holds something.
    """
    rows = [list(r) for r in rows]
    while rows and not any(cell.strip() for cell in rows[-1]):
        rows.pop()
    width = max((len(r) for r in rows), default=0)
    padded = [r + [""] * (width - len(r)) for r in rows]
    columns = [index_to_col_letter(i) for i in range(width)]
    return pd.DataFrame(padded, columns=columns, index=range(1, len(padded) + 1), dtype=str)
