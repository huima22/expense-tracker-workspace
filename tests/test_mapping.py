"""Header & columns step: raw reading, manual mapping, live check, saving as a profile, re-detection."""
import csv
import io
from datetime import date
from pathlib import Path

import openpyxl
import pytest
from streamlit.testing.v1 import AppTest

from core import pipeline
from core.columns import col_letter_to_index
from core.mapping import (
    Mapping, build_profile, check, filename_pattern_for, guess_columns, guess_date_format, guess_profile_name,
    mapping_from_profile, sign_rules_for, suggest_skip,
)
from core.parsers import raw_preview, read_raw, sheet_names
from core.profiles import detect, header_cells
from db import repo
from tests.conftest import FIXTURES

PREAMBLE_CSV = FIXTURES / "dbs_with_preamble.csv"
CARD_XLSX = FIXTURES / "card_negative.xlsx"


def preamble_mapping(**overrides) -> Mapping:
    """What the user picks for dbs_with_preamble.csv."""
    values = dict(skip_rows=5, footer_rows=2, date_col="A", date_format="%d %b %Y", description_col="E",
                  extra_description=["F"], layout="debit_credit", debit_col="C", credit_col="D")
    return Mapping(**{**values, **overrides})


def card_mapping(**overrides) -> Mapping:
    values = dict(skip_rows=1, sheet_name="Statement", date_col="A", date_format="%d/%m/%Y",
                  description_col="B", layout="negative_spend", amount_col="C")
    return Mapping(**{**values, **overrides})


# ---------------------------------------------------------------- 1. raw reading

def test_column_letters():
    assert [col_letter_to_index(c) for c in ("A", "L", "Z", "AA")] == [0, 11, 25, 26]


def test_raw_reader_pads_csv_to_widest_row():
    widest = max(len(r) for r in csv.reader(io.StringIO(PREAMBLE_CSV.read_text(encoding="utf-8"))))
    raw = read_raw(PREAMBLE_CSV.read_bytes(), "csv")
    assert raw.shape == (16, widest) == (16, 7)  # 5 preamble + header + 8 rows + 2 footer
    assert list(raw.columns) == ["A", "B", "C", "D", "E", "F", "G"]
    assert raw.loc[5].tolist() == ["Transaction History", "", "", "", "", "", ""]  # 1-field row padded
    assert raw.loc[6, "A"] == "Transaction Date"
    assert all(isinstance(v, str) for v in raw.to_numpy().ravel())


def test_raw_reader_loads_xlsx():
    data = CARD_XLSX.read_bytes()
    ws = openpyxl.load_workbook(io.BytesIO(data)).active
    widest = max(len(r) for r in ws.iter_rows(values_only=True))
    raw = read_raw(data, "xlsx")
    assert raw.shape == (7, widest) == (7, 3)
    assert raw.loc[1].tolist() == ["Card statement - October 2026", "", ""]
    assert raw.loc[2].tolist() == ["Date", "Description", "Amount"]
    assert raw.loc[3].tolist() == ["2026-10-02", "SHOPEE SINGAPORE", "-120"]
    assert sheet_names(data, "xlsx") == ["Statement"]
    assert sheet_names(PREAMBLE_CSV.read_bytes(), "csv") == []


def test_raw_preview_returns_first_rows():
    preview = raw_preview(PREAMBLE_CSV.read_bytes(), "csv", n=3)
    assert list(preview.index) == [1, 2, 3]
    assert len(raw_preview(PREAMBLE_CSV.read_bytes(), "csv")) == 16  # fewer than 25 rows in the file


# ---------------------------------------------------------------- 2. mapping and live check

def test_skip_and_footer_give_eight_rows():
    raw = read_raw(PREAMBLE_CSV.read_bytes(), "csv")
    result = check(raw, preamble_mapping(), "csv")
    assert result.ok
    assert (result.rows, result.dates_ok, result.amounts_ok, result.expense, result.income) == (8, 8, 8, 7, 1)
    assert [t.txn_date for t in result.txns] == [
        date(2026, 10, d) for d in (1, 3, 5, 8, 12, 15, 20, 22)]
    assert [t.signed_amount_cents for t in result.txns] == [6425, 2350, 1840, 1998, 13260, 780, 150000, 1200]
    assert result.txns[0].description == "NTUC FAIRPRICE BEDOK"  # Ref1 + extra Ref2
    assert result.summary() == "Dates parsed: 8/8 · Amounts parsed: 8/8 · Expense rows: 7 · Income rows: 1"


def test_without_footer_skip_the_check_blocks_save():
    raw = read_raw(PREAMBLE_CSV.read_bytes(), "csv")
    result = check(raw, preamble_mapping(footer_rows=0), "csv")
    assert (result.rows, len(result.txns), len(result.unparsed)) == (10, 8, 2)  # footer lines don't parse
    assert not result.ok  # 80% < 90%
    assert [u.row_number for u in result.unparsed] == [15, 16]


def test_check_reports_missing_fields_and_bad_header_row():
    raw = read_raw(PREAMBLE_CSV.read_bytes(), "csv")
    result = check(raw, Mapping(skip_rows=5, layout="debit_credit"), "csv")
    assert result.missing == ["date column", "description column", "debit column", "credit column"]
    assert not result.ok
    assert check(raw, preamble_mapping(skip_rows=40), "csv").error.startswith("header row 41 is past the end")


def test_negative_spend_layout():
    raw = read_raw(CARD_XLSX.read_bytes(), "xlsx", sheet_name="Statement")
    result = check(raw, card_mapping(), "xlsx")
    assert result.ok
    assert [(t.signed_amount_cents, t.is_debit) for t in result.txns] == [
        (-12000, True), (-1590, True), (-8848, True), (30000, False), (-1998, True)]
    assert [t.txn_date for t in result.txns][:3] == [date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 7)]


def test_indicator_layout():
    text = "Date,Details,Amount,Type\n01/10/2026,COFFEE,4.50,DR\n02/10/2026,SALARY,3000.00,CR\n03/10/2026,X,1.00,??\n"
    raw = read_raw(text.encode(), "csv")
    m = Mapping(date_col="A", date_format="%d/%m/%Y", description_col="B", layout="indicator",
                amount_col="C", indicator_col="D")
    result = check(raw, m, "csv")
    assert [t.is_debit for t in result.txns] == [True, False, None]
    assert result.no_direction == 1
    rules = sign_rules_for(m, 7)
    assert [(r.column_ref, r.operator, r.pattern, r.sign_result) for r in rules] == [
        ("D", "equals", "DR", "expense"), ("D", "equals", "CR", "income")]


@pytest.mark.parametrize("layout, expected", [
    ("positive_spend", [("amount", None, "is_positive", "expense"), ("amount", None, "is_negative", "income")]),
    ("negative_spend", [("amount", None, "is_negative", "expense"), ("amount", None, "is_positive", "income")]),
    ("debit_credit", [("column", "C", "not_empty", "expense"), ("column", "D", "not_empty", "income")]),
])
def test_sign_rules_follow_design_table(layout, expected):
    m = preamble_mapping(layout=layout, amount_col="C")
    rules = sign_rules_for(m, 3)
    assert [(r.target_field, r.column_ref, r.operator, r.sign_result) for r in rules] == expected
    assert [r.priority for r in rules] == [1, 2]
    assert all(r.profile_id == 3 for r in rules)


def test_build_profile_stores_letters_and_signature():
    raw = read_raw(PREAMBLE_CSV.read_bytes(), "csv")
    headers = header_cells(raw, 6)
    profile = build_profile(preamble_mapping(), name="DBS CSV", file_type="csv", headers=headers,
                            filename_pattern=filename_pattern_for("dbs_with_preamble.csv", "csv"))
    assert (profile.header_row, profile.skip_footer_rows, profile.date_format) == (6, 2, "%d %b %Y")
    assert profile.column_map.model_dump() == {
        "date": "A", "description": "E", "amount": None, "debit": "C", "credit": "D", "extra_description": ["F"]}
    assert profile.header_signature == {
        "A": "Transaction Date", "E": "Transaction Ref1", "F": "Transaction Ref2",
        "C": "Debit Amount", "D": "Credit Amount"}
    assert profile.filename_pattern == r"(?i)^dbs.*\.csv$"


@pytest.mark.parametrize("mapping", [
    preamble_mapping(),
    card_mapping(),
    card_mapping(layout="positive_spend"),
    Mapping(date_col="A", date_format="%d/%m/%Y", description_col="B", layout="indicator",
            amount_col="C", indicator_col="D", debit_value="D", credit_value="C"),
])
def test_mapping_round_trips_through_profile(mapping):
    profile = build_profile(mapping, name="P", file_type="csv", headers={}, profile_id=9)
    assert mapping_from_profile(profile, sign_rules_for(mapping, 9), headers={}) == mapping


# ---------------------------------------------------------------- suggestions

def test_suggestions_for_preamble_csv():
    raw = read_raw(PREAMBLE_CSV.read_bytes(), "csv")
    assert suggest_skip(raw) == 5
    assert guess_columns(header_cells(raw, 6)) == {
        "date_col": "A", "description_col": "E", "debit_col": "C", "credit_col": "D"}
    assert guess_date_format(raw, "A", 6) == "%d %b %Y"
    assert guess_profile_name("dbs_with_preamble.csv", "csv") == "DBS CSV"


def test_suggestions_for_card_xlsx():
    raw = read_raw(CARD_XLSX.read_bytes(), "xlsx")
    assert suggest_skip(raw) == 1
    assert guess_columns(header_cells(raw, 2)) == {"date_col": "A", "description_col": "B", "amount_col": "C"}
    assert guess_date_format(raw, "A", 2) == "%d/%m/%Y"
    assert guess_profile_name("card_negative.xlsx", "xlsx") == "Card XLSX"


def test_suggest_skip_without_header_words():
    raw = read_raw(b"Statement\nAcct,123\n01/10/2026,COFFEE,4.50\n02/10/2026,TEA,3.00\n", "csv")
    assert suggest_skip(raw) == 1  # header assumed just above the first data row
    assert suggest_skip(read_raw(b"a\nb\n", "csv")) is None


def test_filename_pattern_matches_same_bank_files():
    import re
    pattern = filename_pattern_for("DBS_with_preamble.csv", "csv")
    assert re.search(pattern, "dbs_Nov2026.CSV")
    assert not re.search(pattern, "citi_Nov2026.csv")
    assert not re.search(pattern, "dbs_Nov2026.xlsx")


# ---------------------------------------------------------------- 3. saving and re-detection

@pytest.fixture
def session(seeded):
    return seeded, repo.create_session(seeded, "October 2026")


def _save(conn, session_id, path: Path, mapping: Mapping, *, profile_id=None, name=None):
    data = path.read_bytes()
    file_type = path.suffix.lstrip(".")
    raw = read_raw(data, file_type, sheet_name=mapping.sheet_name)
    profile = build_profile(mapping, name=name or guess_profile_name(path.name, file_type), file_type=file_type,
                            headers=header_cells(raw, mapping.header_row),
                            filename_pattern=filename_pattern_for(path.name, file_type), profile_id=profile_id)
    return pipeline.save_profile_and_import(conn, session_id=session_id, filename=path.name, data=data,
                                            profile=profile, sign_rules=sign_rules_for(mapping, profile_id or 0))


def test_preamble_file_is_a_near_miss_for_the_seed_profile(session):
    conn, _ = session
    upload = pipeline.read_upload(conn, PREAMBLE_CSV.name, PREAMBLE_CSV.read_bytes())
    assert upload.detected is None  # seed DBS expects Description in column L
    assert upload.near_miss.name == "DBS Savings CSV"


def test_saving_negative_layout_creates_sign_rules(session):
    conn, session_id = session
    profile_id, import_id = _save(conn, session_id, CARD_XLSX, card_mapping())
    profile = repo.get_profile(conn, profile_id)
    assert (profile.name, profile.header_row, profile.sheet_name) == ("Card XLSX", 2, "Statement")
    signs = repo.list_rules(conn, action="sign", profile_id=profile_id)
    assert [(r.target_field, r.operator, r.sign_result, r.profile_id) for r in signs] == [
        ("amount", "is_negative", "expense", profile_id), ("amount", "is_positive", "income", profile_id)]
    stored = repo.list_transactions(conn, import_id)
    assert [s.outcome.is_debit for s in stored] == [True, True, True, False, True]
    assert pipeline.file_summary(conn, import_id).total_cents == 12000 + 1590 + 8848 + 1998


def test_saving_again_replaces_sign_rules(session):
    conn, session_id = session
    profile_id, _ = _save(conn, session_id, CARD_XLSX, card_mapping())
    _save(conn, session_id, CARD_XLSX, card_mapping(layout="positive_spend"), profile_id=profile_id)
    signs = repo.list_rules(conn, action="sign", profile_id=profile_id)
    assert [(r.operator, r.sign_result) for r in signs] == [("is_positive", "expense"), ("is_negative", "income")]


def test_after_saving_the_same_format_is_detected(session):
    conn, session_id = session
    seed_dbs = repo.get_profile_by_name(conn, "DBS Savings CSV")
    profile_id, import_id = _save(conn, session_id, PREAMBLE_CSV, preamble_mapping(),
                                  profile_id=seed_dbs.id, name=seed_dbs.name)  # update the near miss
    assert profile_id == seed_dbs.id
    assert pipeline.file_summary(conn, import_id).total_cents == 26653  # same reference totals

    data = PREAMBLE_CSV.read_bytes()
    for filename in ("dbs_with_preamble.csv", "DBS_Nov2026.csv"):
        upload = pipeline.read_upload(conn, filename, data)
        assert upload.detected is not None and upload.detected.id == profile_id

    card_id, _ = _save(conn, session_id, CARD_XLSX, card_mapping())
    assert pipeline.read_upload(conn, "card_negative.xlsx", CARD_XLSX.read_bytes()).detected.id == card_id


def test_detect_uses_profile_sheet():
    data = CARD_XLSX.read_bytes()
    profile = build_profile(card_mapping(), name="Card", file_type="xlsx",
                            headers={"A": "Date", "B": "Description", "C": "Amount"},
                            filename_pattern=r"(?i)^card", profile_id=1)
    def reader(p):
        return read_raw(data, "xlsx", sheet_name=p.sheet_name)
    assert detect("card_negative.xlsx", [profile], reader).profile == profile
    other_sheet = profile.model_copy(update={"sheet_name": "Missing"})
    assert detect("card_negative.xlsx", [other_sheet], reader).near_miss == other_sheet


# ---------------------------------------------------------------- the step in Streamlit (AppTest)

def _step_script(db_path: str, fixture: str, filename: str) -> None:
    from contextlib import closing
    from pathlib import Path

    from core import pipeline
    from db import repo
    from db.connection import get_conn
    from ui import profile_form

    data = Path(fixture).read_bytes()
    with closing(get_conn(db_path)) as conn:
        session_id = repo.list_sessions(conn)[0]["id"]
        upload = pipeline.read_upload(conn, filename, data)
        profile_form.render(conn, session_id=session_id, filename=filename, data=data,
                            file_type=upload.file_type, sha256=upload.sha256, base=upload.near_miss)


def _step(db_file, fixture: Path) -> AppTest:
    at = AppTest.from_function(_step_script, args=(str(db_file), str(fixture), fixture.name), default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _go(widget) -> AppTest:
    at = widget.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_step_ui_maps_preamble_csv_and_updates_near_miss(session, db_file):
    conn, session_id = session
    at = _step(db_file, PREAMBLE_CSV)
    assert any("header row doesn't" in i.value for i in at.info)  # pre-filled from the seed DBS profile
    assert at.number_input(key="hc_skip").value == 5
    assert at.button(key="hc_save").disabled  # description/debit/credit not in this layout yet

    at.selectbox(key="hc_desc").set_value("E")
    at.selectbox(key="hc_debit").set_value("C")
    at = _go(at.selectbox(key="hc_credit").set_value("D"))
    assert any("Amounts parsed: 8/10" in w.value for w in at.warning)  # footer lines still included
    assert at.button(key="hc_save").disabled

    at = _go(at.number_input(key="hc_footer").set_value(2))
    assert any("Dates parsed: 8/8 · Amounts parsed: 8/8 · Expense rows: 7 · Income rows: 1" in s.value
               for s in at.success)
    assert not at.button(key="hc_save").disabled
    _go(at.button(key="hc_save").click())

    dbs = repo.get_profile_by_name(conn, "DBS Savings CSV")
    assert dbs.column_map.debit == "C" and dbs.skip_footer_rows == 2
    [imp] = repo.list_imports(conn, session_id)
    assert imp["row_count"] == 8
    assert pipeline.read_upload(conn, PREAMBLE_CSV.name, PREAMBLE_CSV.read_bytes()).detected.id == dbs.id


def test_step_ui_new_profile_for_card_xlsx(session, db_file):
    conn, session_id = session
    at = _step(db_file, CARD_XLSX)
    assert any("No saved profile matches" in i.value for i in at.info)
    assert at.number_input(key="hc_skip").value == 1  # suggested
    assert "suggested: 1" in at.number_input(key="hc_skip").label
    assert at.selectbox(key="hc_amount").value == "C"
    assert at.text_input(key="hc_name").value == "Card XLSX"

    at = _go(at.radio(key="hc_layout").set_value("negative_spend"))
    assert any("Expense rows: 4 · Income rows: 1" in s.value for s in at.success)
    _go(at.button(key="hc_save").click())

    card = repo.get_profile_by_name(conn, "Card XLSX")
    signs = repo.list_rules(conn, action="sign", profile_id=card.id)
    assert [(r.operator, r.sign_result) for r in signs] == [("is_negative", "expense"), ("is_positive", "income")]
    assert len(repo.list_imports(conn, session_id)) == 1
