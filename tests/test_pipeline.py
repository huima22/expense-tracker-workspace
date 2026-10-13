"""M4: import pipeline, stored transactions, re-runs, user decisions and per-file summary."""
import pytest

from core import pipeline
from core.models import Category, Rule
from core.parsers import ParseError
from core.profiles import ProfileError
from db import repo
from tests.conftest import CITI_XLSX, citi_profile, citi_sign_rules, dbs_text


@pytest.fixture
def ctx(seeded):
    conn = seeded
    session_id = repo.create_session(conn, "October 2026")
    dbs = repo.get_profile_by_name(conn, "DBS Savings CSV")
    data = dbs_text().encode("utf-8")
    import_id = pipeline.import_file(conn, session_id, "dbs_sample.csv", data, dbs.id)
    ids = {c.name: c.id for c in repo.list_categories(conn)}
    rows = {r.txn.description.split()[0]: r for r in repo.list_transactions(conn, import_id)}
    return {"conn": conn, "session_id": session_id, "profile": dbs, "import_id": import_id,
            "data": data, "ids": ids, "rows": rows}


def totals(conn, import_id):
    summary = pipeline.file_summary(conn, import_id)
    return {t.name: t.cents for t in summary.totals}, summary


def row(ctx, word):
    return repo.get_transaction(ctx["conn"], ctx["rows"][word].id)


# ---------------------------------------------------------------- upload and import

def test_read_upload_detects_profile(seeded):
    upload = pipeline.read_upload(seeded, "dbs_sample.csv", dbs_text().encode())
    assert upload.detected.name == "DBS Savings CSV"
    assert len(upload.sha256) == 64
    assert pipeline.read_upload(seeded, "other.csv", b"a,b\n1,2\n").detected is None
    with pytest.raises(ParseError):
        pipeline.read_upload(seeded, "statement.pdf", b"%PDF")


def test_import_stores_transactions_and_details(ctx):
    imp = repo.get_import(ctx["conn"], ctx["import_id"])
    assert (imp["status"], imp["row_count"], imp["filename"]) == ("draft", 8, "dbs_sample.csv")
    assert imp["headers"]["L"] == "Description"
    assert imp["unparsed"] == []
    stored = repo.list_transactions(ctx["conn"], ctx["import_id"])
    assert [s.txn.description for s in stored][:2] == ["NTUC FAIRPRICE BEDOK", "GRAB*FOOD 4821"]
    assert stored[0].txn.raw_row["M"] == "64.25"
    assert stored[0].outcome.category_id == ctx["ids"]["Groceries"]


def test_import_keeps_unparsed_rows(seeded):
    session_id = repo.create_session(seeded, "S")
    dbs = repo.get_profile_by_name(seeded, "DBS Savings CSV")
    text = dbs_text().rstrip() + "\n31 Foo 2026,,,,,,,,,,,BAD DATE,5.00,\n"
    import_id = pipeline.import_file(seeded, session_id, "dbs.csv", text.encode(), dbs.id)
    [unparsed] = repo.get_import(seeded, import_id)["unparsed"]
    assert unparsed["row_number"] == 15
    assert unparsed["raw_row"]["L"] == "BAD DATE"
    assert "doesn't match" in unparsed["reason"]


def test_import_with_wrong_profile_fails_cleanly(ctx):
    conn = ctx["conn"]
    citi_id = repo.create_profile(conn, citi_profile().model_copy(update={"id": None, "file_type": "csv"}))
    before = len(repo.list_imports(conn, ctx["session_id"]))
    with pytest.raises(ProfileError):
        pipeline.import_file(conn, ctx["session_id"], "x.csv", b"just,one,row\n", citi_id)
    assert len(repo.list_imports(conn, ctx["session_id"])) == before  # nothing half-saved


def test_file_summary_reference_case(ctx):
    by_name, summary = totals(ctx["conn"], ctx["import_id"])
    assert by_name == {"Utilities": 13260, "Groceries": 6425, "Food": 2350, "Subscriptions": 1998,
                       "Transport": 1840, "Uncategorised": 780}
    assert summary.total_cents == 26653
    assert (summary.matched, summary.skipped, summary.needs_category, summary.pending) == (5, 1, 1, 1)
    assert summary.pending_cents == 1200
    assert [t.name for t in summary.totals][0] == "Utilities"  # largest first


# ---------------------------------------------------------------- M4 acceptance: rule for TOAST BOX

def test_rule_for_toast_box_updates_file_total(ctx):
    conn = ctx["conn"]
    rule = Rule(action="categorise", category_id=ctx["ids"]["Food"], operator="contains", pattern="toast",
                priority=20)
    pipeline.save_rule(conn, rule, ctx["session_id"])
    assert row(ctx, "TOAST").outcome.category_id == ctx["ids"]["Food"]
    by_name, summary = totals(conn, ctx["import_id"])
    assert by_name["Food"] == 3130
    assert "Uncategorised" not in by_name
    assert summary.total_cents == 26653


def test_editing_and_disabling_rules_rerun(ctx):
    conn = ctx["conn"]
    netflix = next(r for r in repo.list_rules(conn) if r.pattern == "netflix")
    pipeline.save_rule(conn, netflix.model_copy(update={"category_id": ctx["ids"]["Food"]}), ctx["session_id"])
    assert row(ctx, "NETFLIX.COM").outcome.category_id == ctx["ids"]["Food"]

    repo.set_rule_enabled(conn, netflix.id, False)
    pipeline.rerun_session(conn, ctx["session_id"])
    assert row(ctx, "NETFLIX.COM").outcome.status == "uncategorised"


# ---------------------------------------------------------------- user decisions

def test_keep_and_skip_refund(ctx):
    conn = ctx["conn"]
    pipeline.assign(conn, ctx["rows"]["REFUND"].id, ctx["ids"]["Transport"])
    by_name, summary = totals(conn, ctx["import_id"])
    assert (by_name["Transport"], summary.total_cents, summary.pending) == (640, 25453, 0)

    pipeline.skip(conn, ctx["rows"]["REFUND"].id)
    assert totals(conn, ctx["import_id"])[1].total_cents == 26653


def test_user_decisions_survive_rule_changes_and_reset(ctx):
    conn = ctx["conn"]
    toast_id = ctx["rows"]["TOAST"].id
    pipeline.assign(conn, toast_id, ctx["ids"]["Groceries"])
    pipeline.save_rule(conn, Rule(action="categorise", category_id=ctx["ids"]["Food"], operator="contains",
                                  pattern="toast"), ctx["session_id"])
    assert row(ctx, "TOAST").outcome.category_id == ctx["ids"]["Groceries"]  # user wins

    pipeline.reset(conn, toast_id)
    assert row(ctx, "TOAST").outcome.category_id == ctx["ids"]["Food"]  # rules decide again
    assert row(ctx, "TOAST").outcome.decided_by == "rule"


def test_needs_sign_choice_and_saved_sign_rule(ctx):
    conn = ctx["conn"]
    income_rule = next(r for r in repo.list_rules(conn, action="sign") if r.sign_result == "income")
    repo.delete_rule(conn, income_rule.id)
    pipeline.rerun_session(conn, ctx["session_id"])
    refund_id = ctx["rows"]["REFUND"].id
    assert row(ctx, "REFUND").outcome.status == "needs_sign"

    pipeline.set_sign(conn, refund_id, False)
    outcome = row(ctx, "REFUND").outcome
    assert (outcome.status, outcome.is_debit, outcome.decided_by) == ("uncategorised", False, "user")

    pipeline.reset(conn, refund_id)
    rule = pipeline.set_sign(conn, refund_id, False, save_rule=True)
    assert (rule.operator, rule.sign_result, rule.priority) == ("is_positive", "income", 2)
    # DBS debits are positive too, but the expense rule (priority 1) runs first
    assert row(ctx, "REFUND").outcome.review_reason == "Possible refund"
    assert row(ctx, "NTUC").outcome.is_debit is True


def test_deleted_category_turns_rows_uncategorised(ctx):
    conn = ctx["conn"]
    keep_id = ctx["rows"]["REFUND"].id
    temp = repo.create_category(conn, Category(name="Temp"))
    pipeline.assign(conn, keep_id, temp)
    repo.delete_category(conn, temp)
    outcome = row(ctx, "REFUND").outcome
    assert (outcome.status, outcome.decided_by) == ("uncategorised", "default")


def test_column_rule_by_header_resolves_on_rerun(ctx):
    conn = ctx["conn"]
    rule = Rule(action="skip", target_field="column", column_ref="Reference", operator="equals", pattern="GIRO")
    pipeline.save_rule(conn, rule, ctx["session_id"])
    assert row(ctx, "SP").outcome.status == "skipped"  # 'Reference' header -> column C


def test_xlsx_import(seeded):
    conn = seeded
    profile_id = repo.create_profile(conn, citi_profile().model_copy(update={"id": None}))
    for r in citi_sign_rules(profile_id):
        repo.create_rule(conn, r.model_copy(update={"id": None}))
    session_id = repo.create_session(conn, "S")
    import_id = pipeline.import_file(conn, session_id, CITI_XLSX.name, CITI_XLSX.read_bytes(), profile_id)
    assert pipeline.file_summary(conn, import_id).total_cents == 66048
