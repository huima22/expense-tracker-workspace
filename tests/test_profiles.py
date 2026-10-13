"""M2: detect_profile and normalise on the DBS and Citi fixtures."""
from datetime import date

import pytest

from core.models import Rule
from core.parsers import parse_file
from core.profiles import ProfileError, detect_profile, normalise, parse_amount_cents, parse_date
from tests.conftest import CITI_XLSX, DBS_CSV, citi_profile, citi_sign_rules


def dbs_raw(text: str | None = None):
    data = text.encode("utf-8") if text is not None else DBS_CSV.read_bytes()
    return parse_file("dbs_sample.csv", data)


def citi_raw():
    return parse_file(CITI_XLSX.name, CITI_XLSX.read_bytes(), citi_profile())


# ---------------------------------------------------------------- amount and date parsing

@pytest.mark.parametrize("text, cents", [
    ("18.40", 1840), ("1,500.00", 150000), ("(12.00)", -1200), ("-7.8", -780), ("+3", 300),
    ("S$ 1,234.5", 123450), ("SGD 3", 300), ("$0.99", 99), (" 12.345 ", 1235), ("12-", -1200),
    ("15.9", 1590), (".5", 50),
])
def test_parse_amount_cents(text, cents):
    assert parse_amount_cents(text) == cents


@pytest.mark.parametrize("text", ["", "abc", "1.2.3", "--5", "-(5)", "12 CR", "N/A", "1e5"])
def test_parse_amount_rejects(text):
    with pytest.raises(ValueError):
        parse_amount_cents(text)


@pytest.mark.parametrize("text, fmt, expected", [
    ("03 Oct 2026", "%d %b %Y", date(2026, 10, 3)),
    ("03  OCT 2026", "%d %b %Y", date(2026, 10, 3)),
    ("2026-10-03", "%d/%m/%Y", date(2026, 10, 3)),  # ISO from an XLSX date cell
    ("2026-10-03 14:30:00", "%d/%m/%Y", date(2026, 10, 3)),
])
def test_parse_date(text, fmt, expected):
    assert parse_date(text, fmt) == expected


@pytest.mark.parametrize("text", ["31 Foo 2026", "32 Oct 2026", "", "10/03/2026"])
def test_parse_date_rejects(text):
    with pytest.raises(ValueError):
        parse_date(text, "%d %b %Y")


# ---------------------------------------------------------------- detection

def test_detect_dbs_by_filename_and_header(dbs):
    profile, _ = dbs
    assert detect_profile("DBS_Oct2026.csv", dbs_raw(), [citi_profile(), profile]) == profile


def test_detect_needs_matching_filename(dbs):
    """A renamed file isn't auto-detected; the Header & columns step opens instead."""
    profile, _ = dbs
    assert detect_profile("october.csv", dbs_raw(), [profile]) is None


def test_detect_rejects_wrong_header_even_if_filename_matches(dbs):
    profile, _ = dbs
    other = parse_file("x.csv", b"Date,Description,Amount\n01 Oct 2026,X,1.00\n")
    assert detect_profile("dbs_sample.csv", other, [profile]) is None


def test_detect_requires_matching_file_type(dbs):
    profile, _ = dbs
    assert detect_profile("dbs_sample.xlsx", dbs_raw(), [profile]) is None


def test_detect_citi(dbs):
    profile, _ = dbs
    assert detect_profile("citi_sample.xlsx", citi_raw(), [profile, citi_profile()]).name == "Citi Card XLSX"


def test_detect_prefers_filename_and_header_over_header_only(dbs):
    profile, _ = dbs
    renamed = profile.model_copy(update={"id": 9, "name": "DBS copy", "filename_pattern": r"^nomatch$"})
    assert detect_profile("dbs.csv", dbs_raw(), [renamed, profile]) == profile


def test_detect_without_signature_uses_filename(dbs):
    profile, _ = dbs
    loose = profile.model_copy(update={"header_signature": None})
    assert detect_profile("dbs.csv", dbs_raw(), [loose]) == loose
    assert detect_profile("other.csv", dbs_raw(), [loose]) is None


# ---------------------------------------------------------------- normalise: DBS reference case

def test_dbs_fixture_normalises(dbs):
    profile, sign_rules = dbs
    result = normalise(dbs_raw(), profile, sign_rules)

    assert result.unparsed == []
    assert len(result.txns) == 8  # preamble, header and trailing blank row dropped
    assert [(t.txn_date, t.description, t.signed_amount_cents, t.is_debit) for t in result.txns] == [
        (date(2026, 10, 1), "NTUC FAIRPRICE BEDOK", 6425, True),
        (date(2026, 10, 3), "GRAB*FOOD 4821", 2350, True),
        (date(2026, 10, 5), "GRAB*TRANSPORT SG", 1840, True),
        (date(2026, 10, 8), "NETFLIX.COM", 1998, True),
        (date(2026, 10, 12), "SP SERVICES LTD", 13260, True),
        (date(2026, 10, 15), "TOAST BOX TAMPINES", 780, True),
        (date(2026, 10, 20), "FUND TRANSFER TO <YOUR NAME>", 150000, True),
        (date(2026, 10, 22), "REFUND GRAB*TRANSPORT", 1200, False),
    ]
    assert sum(t.is_debit is True for t in result.txns) == 7
    assert all(t.amount_cents == abs(t.signed_amount_cents) for t in result.txns)
    assert result.txns[0].raw_row["L"] == "NTUC FAIRPRICE BEDOK"
    assert result.headers["M"] == "Debit Amount"


def test_dbs_columns_by_header_name(dbs):
    profile, sign_rules = dbs
    by_name = profile.model_copy(update={"column_map": profile.column_map.model_copy(update={
        "date": "Transaction Date", "description": "description",
        "debit": "Debit Amount", "credit": "Credit Amount"})})
    header_rules = [r.model_copy(update={"column_ref": {"M": "Debit Amount", "N": "Credit Amount"}[r.column_ref]})
                    for r in sign_rules]
    expected = normalise(dbs_raw(), profile, sign_rules).txns
    assert normalise(dbs_raw(), by_name, header_rules).txns == expected


def test_extra_description_columns_are_appended(dbs):
    profile, sign_rules = dbs
    extra = profile.model_copy(update={"column_map": profile.column_map.model_copy(
        update={"extra_description": ["C"]})})
    assert normalise(dbs_raw(), extra, sign_rules).txns[0].description == "NTUC FAIRPRICE BEDOK POS"


def test_bad_rows_are_flagged_unparsed(dbs):
    profile, sign_rules = dbs
    blank = "," * 13
    lines = DBS_CSV.read_text(encoding="utf-8").splitlines()
    lines[7:7] = [
        "31 Foo 2026,,,,,,,,,,,BAD DATE,5.00,",            # row 8
        "02 Oct 2026,,,,,,,,,,,BAD AMOUNT,abc,",            # row 9
        "02 Oct 2026,,,,,,,,,,,BOTH SIDES,1.00,2.00",       # row 10
        "02 Oct 2026,,,,,,,,,,,NO AMOUNT,,",                # row 11
        blank,                                              # blank spacer: dropped silently
    ]
    result = normalise(dbs_raw("\n".join(lines)), profile, sign_rules)

    assert len(result.txns) == 8
    assert [(u.row_number, u.raw_row["L"]) for u in result.unparsed] == [
        (8, "BAD DATE"), (9, "BAD AMOUNT"), (10, "BOTH SIDES"), (11, "NO AMOUNT")]
    reasons = [u.reason for u in result.unparsed]
    assert "doesn't match" in reasons[0]
    assert "not a number" in reasons[1]
    assert "both debit and credit" in reasons[2]
    assert "no amount" in reasons[3]


def test_footer_rows_are_dropped(dbs):
    profile, sign_rules = dbs
    text = DBS_CSV.read_text(encoding="utf-8") + "Total,,,,,,,,,,,,1784.53,12.00\n"
    assert len(normalise(dbs_raw(text), profile, sign_rules).unparsed) == 1  # without skip_footer_rows
    with_footer = profile.model_copy(update={"skip_footer_rows": 1})
    result = normalise(dbs_raw(text), with_footer, sign_rules)
    assert (len(result.txns), result.unparsed) == (8, [])


def test_no_matching_sign_rule_leaves_is_debit_none(dbs):
    profile, sign_rules = dbs
    expense_only = [r for r in sign_rules if r.sign_result == "expense"]
    txns = normalise(dbs_raw(), profile, expense_only).txns
    assert txns[-1].is_debit is None  # the refund matches no sign rule -> needs_sign later
    assert all(t.is_debit is None for t in normalise(dbs_raw(), profile, []).txns)


def test_sign_rules_run_in_priority_order_and_skip_disabled(dbs):
    profile, _ = dbs
    always_income = Rule(id=1, action="sign", sign_result="income", target_field="column",
                         column_ref="A", operator="not_empty", profile_id=profile.id, priority=5)
    always_expense = always_income.model_copy(update={"id": 2, "sign_result": "expense", "priority": 1})
    txns = normalise(dbs_raw(), profile, [always_income, always_expense]).txns
    assert all(t.is_debit is True for t in txns)
    disabled = always_expense.model_copy(update={"enabled": False})
    assert all(t.is_debit is False for t in normalise(dbs_raw(), profile, [always_income, disabled]).txns)


def test_header_row_past_end_of_file(dbs):
    profile, _ = dbs
    with pytest.raises(ProfileError, match="past the end"):
        normalise(parse_file("x.csv", b"a,b\n"), profile)


def test_missing_header_name(dbs):
    profile, _ = dbs
    bad = profile.model_copy(update={"column_map": profile.column_map.model_copy(update={"date": "Posted On"})})
    with pytest.raises(ProfileError, match="Posted On"):
        normalise(dbs_raw(), bad)


# ---------------------------------------------------------------- hashes

def test_row_hashes_stable_and_distinct(dbs):
    profile, sign_rules = dbs
    first = [t.row_hash for t in normalise(dbs_raw(), profile, sign_rules).txns]
    second = [t.row_hash for t in normalise(dbs_raw(), profile, sign_rules).txns]
    assert first == second
    assert len(set(first)) == 8
    other_profile = profile.model_copy(update={"id": profile.id + 100})
    assert set(first).isdisjoint(t.row_hash for t in normalise(dbs_raw(), other_profile, sign_rules).txns)


def test_identical_rows_get_distinct_hashes(dbs):
    profile, sign_rules = dbs
    coffee = "16 Oct 2026,,,,,,,,,,,TOAST BOX TAMPINES,2.10,"
    text = DBS_CSV.read_text(encoding="utf-8").rstrip() + f"\n{coffee}\n{coffee}\n"
    txns = normalise(dbs_raw(text), profile, sign_rules).txns
    assert len(txns) == 10
    assert txns[-1].row_hash != txns[-2].row_hash


# ---------------------------------------------------------------- normalise: Citi (XLSX)

def test_citi_fixture_normalises():
    result = normalise(citi_raw(), citi_profile(), citi_sign_rules())
    assert result.unparsed == []  # 'Total' footer dropped by skip_footer_rows
    assert [(t.txn_date, t.signed_amount_cents, t.is_debit) for t in result.txns] == [
        (date(2026, 10, 2), 12000, True),
        (date(2026, 10, 5), 1590, True),
        (date(2026, 10, 7), 8848, True),
        (date(2026, 10, 11), 4500, True),   # text date, %d/%m/%Y
        (date(2026, 10, 14), 39110, True),
        (date(2026, 10, 20), -50000, False),
    ]
    assert result.txns[-1].amount_cents == 50000
    assert sum(t.amount_cents for t in result.txns if t.is_debit) == 66048
