"""CSV parser. Uses the csv module, not pd.read_csv: bank preambles have fewer
fields than the data rows, which makes pandas fail."""
from __future__ import annotations

import csv
import io

import pandas as pd

from core.parsers.base import ParseError, rows_to_frame

# Tried in order after the profile's own encoding (if any).
FALLBACK_ENCODINGS = ("utf-8-sig", "cp1252")


def _candidates(encoding: str | None) -> list[str]:
    ordered = [encoding] if encoding else []
    return ordered + [e for e in FALLBACK_ENCODINGS if e != encoding]


def detect_encoding(data: bytes, encoding: str | None = None) -> str:
    """The first encoding (profile's, then utf-8-sig, then cp1252) that decodes the file."""
    for candidate in _candidates(encoding):
        try:
            data.decode(candidate)
            return candidate
        except UnicodeDecodeError:
            continue
    raise ParseError(f"can't decode the file as {' or '.join(_candidates(encoding))}")


class CsvParser:
    extensions = (".csv",)

    def read(self, data: bytes, *, sheet_name: str | None = None, encoding: str | None = None) -> pd.DataFrame:
        text = data.decode(detect_encoding(data, encoding))
        try:
            rows = list(csv.reader(io.StringIO(text, newline="")))
        except csv.Error as exc:
            raise ParseError(f"invalid CSV: {exc}") from exc
        return rows_to_frame(rows)

    def sheet_names(self, data: bytes) -> list[str]:
        return []
