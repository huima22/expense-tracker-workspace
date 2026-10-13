"""M2: CSV/XLSX parsers, registry and column letters."""
import io

import openpyxl
import pytest

from core.parsers import ParseError, detect_encoding, file_type_of, get_parser, parse_file
from core.profiles import col_letter_to_index, index_to_col_letter
from tests.conftest import CITI_XLSX, DBS_CSV, citi_profile


# ---------------------------------------------------------------- column letters

@pytest.mark.parametrize("letter, index", [("A", 0), ("L", 11), ("Z", 25), ("AA", 26), ("AZ", 51), ("BA", 52)])
def test_col_letter_to_index(letter, index):
    assert col_letter_to_index(letter) == index
    assert index_to_col_letter(index) == letter


def test_col_letters_round_trip():
    assert all(col_letter_to_index(index_to_col_letter(i)) == i for i in range(2000))


@pytest.mark.parametrize("bad", ["", "a", "A1", "ABCD", "Description"])
def test_bad_col_letter_rejected(bad):
    with pytest.raises(ValueError):
        col_letter_to_index(bad)


# ---------------------------------------------------------------- registry

def test_registry_picks_parser_by_extension():
    assert ".csv" in get_parser("x.CSV").extensions
    assert ".xlsx" in get_parser("dir/x.xlsx").extensions
    assert file_type_of("DBS Oct.CSV") == "csv"


@pytest.mark.parametrize("name", ["statement.pdf", "statement.xls", "noext"])
def test_unsupported_extension(name):
    with pytest.raises(ParseError, match="unsupported file type"):
        parse_file(name, b"data")


# ---------------------------------------------------------------- CSV

def test_csv_frame_shape_and_types():
    raw = parse_file(DBS_CSV.name, DBS_CSV.read_bytes())
    assert list(raw.columns) == [index_to_col_letter(i) for i in range(14)]  # A..N
    assert list(raw.index) == list(range(1, 15))  # 5 preamble + header + 8 rows; trailing blank trimmed
    assert raw.loc[1, "A"] == "Account Details For:"
    assert raw.loc[1, "C"] == ""  # short preamble row padded
    assert raw.loc[6, "L"] == "Description"
    assert raw.loc[13, "M"] == "1,500.00"  # cells kept exactly as text
    assert all(isinstance(v, str) for v in raw.to_numpy().ravel())


def test_csv_encoding_from_profile_and_fallback():
    data = "Date,Desc,Amt\n01/10/2026,CAFÉ €,1.00\n".encode("cp1252")
    profile = citi_profile().model_copy(update={"encoding": "cp1252"})
    assert parse_file("x.csv", data, profile).loc[2, "B"] == "CAFÉ €"
    assert parse_file("x.csv", data).loc[2, "B"] == "CAFÉ €"  # no profile: utf-8-sig fails, cp1252 works
    utf8_profile = citi_profile().model_copy(update={"encoding": "utf-8"})
    assert parse_file("x.csv", data, utf8_profile).loc[2, "B"] == "CAFÉ €"  # profile's encoding, then fallbacks
    assert detect_encoding(data) == "cp1252"
    assert detect_encoding("é".encode("utf-8")) == "utf-8-sig"


def test_csv_undecodable_file():
    with pytest.raises(ParseError, match="can't decode"):
        parse_file("x.csv", b"\x81\x8d\x90 not text in utf-8 or cp1252")


def test_csv_utf8_bom_is_stripped():
    raw = parse_file("x.csv", "﻿Date,Amt\n".encode("utf-8"))
    assert raw.loc[1, "A"] == "Date"


def test_empty_csv():
    assert parse_file("x.csv", b"").empty


# ---------------------------------------------------------------- XLSX

def test_xlsx_cells_become_text():
    raw = parse_file(CITI_XLSX.name, CITI_XLSX.read_bytes())
    assert list(raw.columns) == ["A", "B", "C", "D"]
    assert raw.loc[4, "A"] == "Transaction Date"
    assert raw.loc[5, "A"] == "2026-10-02"  # Excel date cell -> ISO text
    assert raw.loc[8, "A"] == "11/10/2026"  # text date kept as is
    assert raw.loc[5, "D"] == "120"
    assert raw.loc[6, "D"] == "15.9"
    assert raw.loc[10, "D"] == "-500"
    assert raw.loc[11, "A"] == "Total"
    assert all(isinstance(v, str) for v in raw.to_numpy().ravel())


def test_xlsx_sheet_selection():
    data = CITI_XLSX.read_bytes()
    assert parse_file("c.xlsx", data, citi_profile()).loc[4, "C"] == "Description"
    notes = parse_file("c.xlsx", data, citi_profile().model_copy(update={"sheet_name": "Notes"}))
    assert notes.loc[1, "A"] == "Fake data for tests"
    with pytest.raises(ParseError, match="sheet 'Missing' not found"):
        parse_file("c.xlsx", data, citi_profile().model_copy(update={"sheet_name": "Missing"}))


def test_xlsx_datetime_with_time_and_bool():
    wb = openpyxl.Workbook()
    from datetime import datetime
    wb.active.append([datetime(2026, 10, 3, 14, 30), True, None, "x"])
    buf = io.BytesIO()
    wb.save(buf)
    raw = parse_file("t.xlsx", buf.getvalue())
    assert raw.loc[1].tolist() == ["2026-10-03 14:30:00", "TRUE", "", "x"]


def test_corrupt_xlsx():
    with pytest.raises(ParseError, match="not a readable .xlsx"):
        parse_file("bad.xlsx", b"this is not a zip")
