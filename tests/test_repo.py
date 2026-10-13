"""M1: repo CRUD for categories, profiles, rules, sessions and imports, plus model validation."""
import sqlite3

import pytest
from pydantic import ValidationError

from core.models import Category, ColumnMap, Profile, Rule
from db import repo


def make_profile(name="Test Bank CSV", **overrides):
    data = {
        "name": name,
        "file_type": "csv",
        "filename_pattern": r"(?i)test.*\.csv$",
        "header_row": 3,
        "header_signature": {"A": "Date", "C": "Details"},
        "column_map": {"date": "A", "description": "C", "amount": "D", "extra_description": ["E"]},
        "date_format": "%d/%m/%Y",
    }
    return Profile.model_validate({**data, **overrides})


# ---------------------------------------------------------------- categories

def test_category_crud(conn):
    cid = repo.create_category(conn, Category(name="Food", color="#E8A33D", sort_order=2))
    assert repo.get_category(conn, cid) == Category(id=cid, name="Food", color="#E8A33D", sort_order=2)

    repo.update_category(conn, Category(id=cid, name="Dining", color="#000000", sort_order=5, is_active=False))
    got = repo.get_category(conn, cid)
    assert (got.name, got.color, got.sort_order, got.is_active) == ("Dining", "#000000", 5, False)

    assert repo.delete_category(conn, cid) is True
    assert repo.get_category(conn, cid) is None
    assert repo.delete_category(conn, cid) is False


def test_list_categories_sorted_and_active_filter(conn):
    repo.create_category(conn, Category(name="B", sort_order=2))
    repo.create_category(conn, Category(name="A", sort_order=1))
    repo.create_category(conn, Category(name="Hidden", sort_order=0, is_active=False))
    assert [c.name for c in repo.list_categories(conn)] == ["Hidden", "A", "B"]
    assert [c.name for c in repo.list_categories(conn, active_only=True)] == ["A", "B"]


def test_category_name_is_unique(conn):
    repo.create_category(conn, Category(name="Food"))
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_category(conn, Category(name="Food"))


def test_update_missing_category_raises(conn):
    with pytest.raises(LookupError):
        repo.update_category(conn, Category(id=999, name="Ghost"))


def test_upsert_category_keeps_id(conn):
    cid = repo.upsert_category(conn, Category(name="Food", color="#111111"))
    assert repo.upsert_category(conn, Category(name="Food", color="#222222")) == cid
    assert repo.get_category(conn, cid).color == "#222222"
    assert len(repo.list_categories(conn)) == 1


@pytest.mark.parametrize("bad", [{"name": ""}, {"name": "X", "color": "red"}, {"name": "X", "extra": 1}])
def test_invalid_category_rejected(bad):
    with pytest.raises(ValidationError):
        Category.model_validate(bad)


# ---------------------------------------------------------------- profiles

def test_profile_crud_round_trips_json_fields(conn):
    pid = repo.create_profile(conn, make_profile())
    got = repo.get_profile(conn, pid)
    assert got == make_profile().model_copy(update={"id": pid})
    assert got.column_map.extra_description == ["E"]
    assert got.header_signature == {"A": "Date", "C": "Details"}

    repo.update_profile(conn, got.model_copy(update={"header_row": 7, "header_signature": None}))
    got = repo.get_profile(conn, pid)
    assert (got.header_row, got.header_signature) == (7, None)

    assert repo.get_profile_by_name(conn, "Test Bank CSV").id == pid
    assert [p.name for p in repo.list_profiles(conn)] == ["Test Bank CSV"]
    assert repo.delete_profile(conn, pid) is True
    assert repo.get_profile(conn, pid) is None


def test_upsert_profile_keeps_id(conn):
    pid = repo.upsert_profile(conn, make_profile())
    assert repo.upsert_profile(conn, make_profile(header_row=9)) == pid
    assert repo.get_profile(conn, pid).header_row == 9


@pytest.mark.parametrize("overrides", [
    {"file_type": "pdfx"},
    {"filename_pattern": "(unclosed"},
    {"header_row": 0},
    {"skip_footer_rows": -1},
    {"date_format": "dd/mm/yyyy"},
    {"encoding": "not-a-codec"},
    {"column_map": {"date": "A", "description": "C"}},                           # no amount source
    {"column_map": {"date": "A", "description": "C", "amount": "D", "debit": "E"}},  # both
])
def test_invalid_profile_rejected(overrides):
    with pytest.raises(ValidationError):
        make_profile(**overrides)


def test_debit_credit_column_map_is_valid():
    assert ColumnMap(date="A", description="L", debit="M", credit="N").amount is None


# ---------------------------------------------------------------- rules

@pytest.fixture
def ids(conn):
    return {
        "food": repo.create_category(conn, Category(name="Food")),
        "transport": repo.create_category(conn, Category(name="Transport")),
        "dbs": repo.create_profile(conn, make_profile("DBS")),
        "citi": repo.create_profile(conn, make_profile("Citi")),
    }


def test_rule_crud(conn, ids):
    rule = Rule(action="categorise", category_id=ids["food"], operator="contains", pattern="toast box", priority=20)
    rid = repo.create_rule(conn, rule)
    assert repo.get_rule(conn, rid) == rule.model_copy(update={"id": rid})

    repo.update_rule(conn, repo.get_rule(conn, rid).model_copy(update={"priority": 5, "note": "breakfast"}))
    got = repo.get_rule(conn, rid)
    assert (got.priority, got.note) == (5, "breakfast")

    repo.set_rule_enabled(conn, rid, False)
    assert repo.get_rule(conn, rid).enabled is False

    assert repo.delete_rule(conn, rid) is True
    assert repo.get_rule(conn, rid) is None


def test_list_rules_order_scope_and_filters(conn, ids):
    def add(**kw):
        return repo.create_rule(conn, Rule(**kw))

    cat_general = add(action="categorise", category_id=ids["transport"], operator="contains", pattern="grab", priority=20)
    cat_specific = add(action="categorise", category_id=ids["food"], operator="contains", pattern="grab*food", priority=10)
    skip = add(action="skip", operator="contains", pattern="me", priority=1)
    sign_dbs = add(action="sign", sign_result="expense", target_field="column", column_ref="M",
                   operator="not_empty", profile_id=ids["dbs"], priority=1)
    sign_citi = add(action="sign", sign_result="expense", target_field="amount",
                    operator="is_positive", profile_id=ids["citi"], priority=1)
    review = add(action="review", operator="contains", pattern="refund", priority=1, note="Possible refund")
    disabled = add(action="skip", operator="contains", pattern="old", priority=2, enabled=False)

    # sign, review, skip, categorise; by priority within each action
    assert [r.id for r in repo.list_rules(conn)] == [
        sign_dbs, sign_citi, review, skip, disabled, cat_specific, cat_general]
    assert [r.id for r in repo.list_rules(conn, profile_id=ids["dbs"], enabled_only=True)] == [
        sign_dbs, review, skip, cat_specific, cat_general]
    assert [r.id for r in repo.list_rules(conn, action="sign")] == [sign_dbs, sign_citi]


def test_deleting_category_or_profile_cascades_to_rules(conn, ids):
    cat_rule = repo.create_rule(conn, Rule(action="categorise", category_id=ids["food"], operator="contains", pattern="x"))
    sign_rule = repo.create_rule(conn, Rule(action="sign", sign_result="income", target_field="amount",
                                            operator="is_negative", profile_id=ids["dbs"]))
    repo.delete_category(conn, ids["food"])
    repo.delete_profile(conn, ids["dbs"])
    assert repo.get_rule(conn, cat_rule) is None
    assert repo.get_rule(conn, sign_rule) is None


def test_upsert_rule_matches_on_condition(conn, ids):
    rule = Rule(action="categorise", category_id=ids["transport"], operator="contains", pattern="grab", priority=20)
    rid = repo.upsert_rule(conn, rule)
    moved = rule.model_copy(update={"category_id": ids["food"], "priority": 30})
    assert repo.upsert_rule(conn, moved) == rid
    got = repo.get_rule(conn, rid)
    assert (got.category_id, got.priority) == (ids["food"], 30)

    # Same pattern scoped to a profile is a different rule.
    scoped = rule.model_copy(update={"profile_id": ids["dbs"]})
    assert repo.upsert_rule(conn, scoped) != rid


def test_repo_revalidates_models_changed_after_creation(conn, ids):
    rule = Rule(action="skip", operator="contains", pattern="x")
    rule.action = "categorise"  # pydantic doesn't validate assignment; repo must catch it
    with pytest.raises(ValidationError):
        repo.create_rule(conn, rule)


@pytest.mark.parametrize("data", [
    {"action": "categorise", "operator": "contains", "pattern": "x"},                        # no category
    {"action": "skip", "operator": "contains", "pattern": "x", "category_id": 1},            # category on skip
    {"action": "sign", "operator": "is_negative", "target_field": "amount", "profile_id": 1},  # no sign_result
    {"action": "sign", "sign_result": "expense", "operator": "is_negative", "target_field": "amount"},  # no profile
    {"action": "skip", "operator": "contains", "pattern": "x", "sign_result": "income"},
    {"action": "skip", "operator": "regex", "pattern": "grab(("},                            # bad regex
    {"action": "skip", "operator": "contains"},                                             # no pattern
    {"action": "skip", "operator": "contains", "pattern": "x", "target_field": "amount"},   # text op on amount
    {"action": "skip", "operator": "is_negative"},                                          # amount op on description
    {"action": "skip", "operator": "is_negative", "target_field": "amount", "pattern": "1"},  # pattern not allowed
    {"action": "skip", "operator": "not_empty"},                                            # needs column
    {"action": "skip", "operator": "not_empty", "target_field": "column"},                  # no column_ref
    {"action": "skip", "operator": "contains", "pattern": "x", "column_ref": "M"},          # column_ref w/o column
    {"action": "skip", "operator": "amount_between", "target_field": "amount", "pattern": "10"},
    {"action": "skip", "operator": "amount_between", "target_field": "amount", "pattern": "50,10"},
    {"action": "skip", "operator": "amount_between", "target_field": "amount", "pattern": "a,b"},
    {"action": "skip", "operator": "contains", "pattern": "x", "priority": -1},
    {"action": "ignore", "operator": "contains", "pattern": "x"},
])
def test_invalid_rule_rejected(data):
    with pytest.raises(ValidationError):
        Rule.model_validate(data)


@pytest.mark.parametrize("data", [
    {"action": "review", "operator": "contains", "pattern": "refund", "note": "Possible refund"},
    {"action": "skip", "operator": "amount_between", "target_field": "amount", "pattern": "0, 5"},
    {"action": "skip", "operator": "equals", "target_field": "column", "column_ref": "C", "pattern": "CR"},
    {"action": "skip", "operator": "regex", "pattern": r"grab\*?food"},
])
def test_valid_rule_accepted(data):
    Rule.model_validate(data)


# ---------------------------------------------------------------- sessions and imports

def test_session_crud(conn):
    sid = repo.create_session(conn, "  October 2026 ")
    session = repo.get_session(conn, sid)
    assert session["name"] == "October 2026"
    assert session["consolidated_at"] is None

    repo.rename_session(conn, sid, "Oct 2026")
    repo.mark_session_consolidated(conn, sid)
    session = repo.get_session(conn, sid)
    assert session["name"] == "Oct 2026"
    assert session["consolidated_at"] is not None

    newer = repo.create_session(conn, "November 2026")
    assert [s["id"] for s in repo.list_sessions(conn)] == [newer, sid]

    with pytest.raises(ValueError):
        repo.create_session(conn, "   ")
    with pytest.raises(LookupError):
        repo.mark_session_consolidated(conn, 999)


def test_import_crud_and_cascades(conn):
    pid = repo.create_profile(conn, make_profile())
    sid = repo.create_session(conn, "October 2026")
    iid = repo.create_import(conn, session_id=sid, profile_id=pid, filename="test.csv",
                             file_sha256="ab" * 32, row_count=8)
    imp = repo.get_import(conn, iid)
    assert (imp["status"], imp["row_count"], imp["filename"]) == ("draft", 8, "test.csv")

    repo.commit_import(conn, iid)
    assert repo.get_import(conn, iid)["status"] == "committed"
    assert [i["id"] for i in repo.list_imports(conn, sid)] == [iid]

    # A profile in use by an import can't be deleted (FK, no cascade).
    with pytest.raises(sqlite3.IntegrityError):
        repo.delete_profile(conn, pid)

    # Deleting the session removes its imports.
    assert repo.delete_session(conn, sid) is True
    assert repo.get_import(conn, iid) is None
    assert repo.delete_profile(conn, pid) is True


def test_import_requires_existing_session(conn):
    pid = repo.create_profile(conn, make_profile())
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_import(conn, session_id=999, profile_id=pid, filename="x.csv", file_sha256="x", row_count=0)
