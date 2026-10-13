"""M3: rule engine on the seed rules and fixtures (DESIGN.md section 10 checklist)."""
import csv
import io
from collections import defaultdict
from datetime import date

import pytest
from pydantic import ValidationError

from core.models import Outcome, Profile, Rule, Txn
from core.parsers import parse_file
from core.profiles import normalise
from core.rules import (
    apply_rules, assign_category, choose_sign, conflicts, skip_row, suggest_pattern, suggest_rule, test_rule,
)
from db import repo
from tests.conftest import CITI_XLSX, citi_profile, citi_sign_rules, dbs_text


# ---------------------------------------------------------------- helpers

def category_totals(txns, outcomes, names):
    """Per-category totals in cents, with the consolidated query's semantics (DESIGN.md section 5):
    expenses add, kept income subtracts, pending and skipped rows are left out."""
    totals = defaultdict(int)
    for txn, o in zip(txns, outcomes):
        if o.status == "categorised" or (o.status == "uncategorised" and o.is_debit is True):
            name = names.get(o.category_id, "Uncategorised")
            totals[name] += txn.amount_cents if o.is_debit else -txn.amount_cents
    return dict(totals)


@pytest.fixture
def env(seeded):
    """Seeded DB, DBS profile, all rules, category names and the parsed DBS fixture."""
    profile = repo.get_profile_by_name(seeded, "DBS Savings CSV")
    raw = parse_file("dbs_sample.csv", dbs_text().encode("utf-8"))
    rules = repo.list_rules(seeded)
    txns = normalise(raw, profile, rules).txns
    names = {c.id: c.name for c in repo.list_categories(seeded)}
    ids = {name: cid for cid, name in names.items()}
    return {"conn": seeded, "profile": profile, "rules": rules, "txns": txns, "names": names, "ids": ids}


def by_description(txns, outcomes):
    return {t.description.split()[0]: o for t, o in zip(txns, outcomes)}


EXPECTED_DBS = {
    "Utilities": 13260, "Groceries": 6425, "Food": 2350,
    "Subscriptions": 1998, "Transport": 1840, "Uncategorised": 780,
}


# ---------------------------------------------------------------- M3 acceptance: seed rules on DBS

def test_seed_rules_on_dbs_fixture(env):
    outcomes = apply_rules(env["txns"], env["rules"], env["profile"].id)
    names = env["names"]
    result = [(t.description, o.status, names.get(o.category_id), o.review_reason, o.is_debit)
              for t, o in zip(env["txns"], outcomes)]
    own_name_row = env["txns"][6].description
    assert result == [
        ("NTUC FAIRPRICE BEDOK", "categorised", "Groceries", None, True),
        ("GRAB*FOOD 4821", "categorised", "Food", None, True),            # grab*food (10) before grab (20)
        ("GRAB*TRANSPORT SG", "categorised", "Transport", None, True),
        ("NETFLIX.COM", "categorised", "Subscriptions", None, True),
        ("SP SERVICES LTD", "categorised", "Utilities", None, True),
        ("TOAST BOX TAMPINES", "uncategorised", None, None, True),
        (own_name_row, "skipped", None, None, True),                       # own-account transfer
        ("REFUND GRAB*TRANSPORT", "uncategorised", None, "Possible refund", False),
    ]


def test_matched_rule_and_decided_by_are_recorded(env):
    outcomes = by_description(env["txns"], apply_rules(env["txns"], env["rules"], env["profile"].id))
    rules = {r.id: r for r in env["rules"]}
    assert rules[outcomes["GRAB*FOOD"].matched_rule_id].pattern == "grab*food"
    assert rules[outcomes["REFUND"].matched_rule_id].action == "review"
    assert (outcomes["TOAST"].matched_rule_id, outcomes["TOAST"].decided_by) == (None, "default")
    assert outcomes["NTUC"].decided_by == "rule"


def test_reference_totals_with_refund_pending(env):
    outcomes = apply_rules(env["txns"], env["rules"], env["profile"].id)
    totals = category_totals(env["txns"], outcomes, env["names"])
    assert totals == EXPECTED_DBS
    assert sum(totals.values()) == 26653  # 266.53


def test_keep_refund_as_transport(env):
    outcomes = apply_rules(env["txns"], env["rules"], env["profile"].id)
    outcomes[7] = assign_category(outcomes[7], env["ids"]["Transport"])
    totals = category_totals(env["txns"], outcomes, env["names"])
    assert totals["Transport"] == 640  # 18.40 - 12.00
    assert sum(totals.values()) == 25453


def test_skip_refund_leaves_total(env):
    outcomes = apply_rules(env["txns"], env["rules"], env["profile"].id)
    outcomes[7] = skip_row(outcomes[7])
    assert outcomes[7].decided_by == "user"
    assert sum(category_totals(env["txns"], outcomes, env["names"]).values()) == 26653


def test_make_rule_for_toast_box_moves_it_to_food(env):
    toast = env["txns"][5]
    draft = suggest_rule(toast, env["ids"]["Food"])
    assert (draft.pattern, draft.operator, draft.priority) == ("toast", "contains", 20)
    repo.create_rule(env["conn"], draft)

    outcomes = apply_rules(env["txns"], repo.list_rules(env["conn"]), env["profile"].id)
    totals = category_totals(env["txns"], outcomes, env["names"])
    assert totals["Food"] == 2350 + 780
    assert "Uncategorised" not in totals
    assert sum(totals.values()) == 26653


def test_credits_never_counted(env):
    """Income not caught by a review rule is skipped by default, whatever its description."""
    salary = env["txns"][7].model_copy(update={"description": "SALARY GRAB HOLDINGS"})
    [outcome] = apply_rules([salary], env["rules"], env["profile"].id)
    assert (outcome.status, outcome.decided_by, outcome.is_debit) == ("skipped", "default", False)


def test_negative_spend_layout_gives_identical_totals(env):
    """Same data with one amount column, spend negative, and is_negative -> expense sign rules."""
    rows = list(csv.reader(io.StringIO(dbs_text())))
    for row in rows[6:]:
        if row:
            debit, credit = row[12], row[13]
            row[12], row[13] = (f"-{debit}" if debit else credit), ""
    out = io.StringIO()
    csv.writer(out).writerows(rows)

    profile = Profile.model_validate({
        **env["profile"].model_dump(), "id": 50, "name": "Negative spend",
        "column_map": {"date": "A", "description": "L", "amount": "M"}})
    sign_rules = [
        Rule(id=201, action="sign", sign_result="expense", target_field="amount", operator="is_negative",
             profile_id=50, priority=1),
        Rule(id=202, action="sign", sign_result="income", target_field="amount", operator="is_positive",
             profile_id=50, priority=2),
    ]
    rules = [r for r in env["rules"] if r.action != "sign"] + sign_rules
    txns = normalise(parse_file("neg.csv", out.getvalue().encode()), profile, rules).txns
    assert txns[0].signed_amount_cents == -6425
    outcomes = apply_rules(txns, rules, 50)
    assert category_totals(txns, outcomes, env["names"]) == EXPECTED_DBS


def test_no_sign_rule_match_gives_needs_sign(env):
    expense_only = [r for r in env["rules"] if r.sign_result != "income"]
    outcomes = apply_rules(env["txns"], expense_only, env["profile"].id)
    assert (outcomes[7].status, outcomes[7].is_debit) == ("needs_sign", None)
    totals = category_totals(env["txns"], outcomes, env["names"])
    assert sum(totals.values()) == 26653  # excluded from totals


def test_choose_sign_on_needs_sign_row(env):
    expense_only = [r for r in env["rules"] if r.sign_result != "income"]
    refund = env["txns"][7]
    outcome = choose_sign(refund, False, expense_only, env["profile"].id)
    assert (outcome.status, outcome.review_reason, outcome.decided_by) == ("uncategorised", "Possible refund", "user")
    # the choice survives a re-run
    outcomes = apply_rules(env["txns"], expense_only, env["profile"].id,
                           previous=[None] * 7 + [outcome])
    assert outcomes[7] == outcome


def test_citi_consolidates_with_dbs(env):
    profile = citi_profile()
    raw = parse_file(CITI_XLSX.name, CITI_XLSX.read_bytes(), profile)
    rules = env["rules"] + citi_sign_rules()
    txns = normalise(raw, profile, rules).txns
    outcomes = apply_rules(txns, rules, profile.id)
    citi = category_totals(txns, outcomes, env["names"])
    assert citi == {"Food": 1590, "Uncategorised": 64458}  # GRAB*FOOD; the rest unmatched
    assert outcomes[-1].status == "skipped"  # card payment (income)

    dbs = category_totals(env["txns"], apply_rules(env["txns"], env["rules"], env["profile"].id), env["names"])
    assert sum(citi.values()) == 66048
    assert sum(citi.values()) + sum(dbs.values()) == 92701  # 927.01


# ---------------------------------------------------------------- order, scope, user decisions

def make_txn(description, signed=1000):
    return Txn(txn_date=date(2026, 10, 1), description=description, signed_amount_cents=signed,
               amount_cents=abs(signed), row_hash=description, raw_row={"M": "x"})


SIGN = Rule(id=1, action="sign", sign_result="expense", target_field="column", column_ref="M",
            operator="not_empty", profile_id=7)


def cat_rule(rule_id, category_id, pattern, priority=20, **kw):
    return Rule(id=rule_id, action="categorise", category_id=category_id, operator="contains",
                pattern=pattern, priority=priority, **kw)


def test_priority_then_id_breaks_ties():
    txns = [make_txn("COFFEE SHOP")]
    later_id_low_priority = cat_rule(9, 100, "coffee", priority=5)
    tie_a, tie_b = cat_rule(3, 200, "shop", priority=10), cat_rule(2, 300, "coffee", priority=10)
    assert apply_rules(txns, [SIGN, tie_a, tie_b], 7)[0].category_id == 300  # same priority: lower id
    assert apply_rules(txns, [SIGN, tie_a, tie_b, later_id_low_priority], 7)[0].category_id == 100


def test_disabled_rules_are_ignored():
    txns = [make_txn("COFFEE")]
    rule = cat_rule(2, 100, "coffee", enabled=False)
    assert apply_rules(txns, [SIGN, rule], 7)[0].status == "uncategorised"


def test_profile_scope():
    txns = [make_txn("COFFEE")]
    other_profile = cat_rule(2, 100, "coffee", profile_id=8)
    this_profile = cat_rule(3, 200, "coffee", profile_id=7)
    assert apply_rules(txns, [SIGN, other_profile], 7)[0].status == "uncategorised"
    assert apply_rules(txns, [SIGN, other_profile, this_profile], 7)[0].category_id == 200
    other_sign = SIGN.model_copy(update={"profile_id": 8})
    assert apply_rules(txns, [other_sign], 7)[0].status == "needs_sign"  # sign rules are per profile


def test_review_runs_before_skip_and_skip_before_categorise():
    txns = [make_txn("REFUND COFFEE")]
    review = Rule(id=2, action="review", operator="contains", pattern="refund", note="Possible refund")
    skip = Rule(id=3, action="skip", operator="contains", pattern="coffee")
    cat = cat_rule(4, 100, "coffee", priority=1)
    assert apply_rules(txns, [SIGN, cat, skip, review], 7)[0].review_reason == "Possible refund"
    assert apply_rules(txns, [SIGN, cat, skip], 7)[0].status == "skipped"


def test_review_rule_without_note_gets_default_reason():
    review = Rule(id=2, action="review", operator="contains", pattern="coffee")
    assert apply_rules([make_txn("COFFEE")], [SIGN, review], 7)[0].review_reason == "Needs review"


def test_user_decisions_and_duplicates_survive_reruns():
    txns = [make_txn("A COFFEE"), make_txn("B COFFEE"), make_txn("C COFFEE")]
    rules = [SIGN, cat_rule(2, 100, "coffee")]
    first = apply_rules(txns, rules, 7)
    user = assign_category(first[0], 999)
    duplicate = Outcome(status="duplicate", is_debit=True)
    rerun = apply_rules(txns, rules + [cat_rule(3, 555, "coffee", priority=1)], 7,
                        previous=[user, duplicate, first[2]])
    assert rerun[0] == user
    assert rerun[1] == duplicate
    assert rerun[2].category_id == 555  # rule decisions follow the new rules


def test_previous_length_must_match():
    with pytest.raises(ValueError, match="previous has 1 outcomes for 2"):
        apply_rules([make_txn("A"), make_txn("B")], [SIGN], 7, previous=[None])


def test_user_decisions_need_a_direction():
    pending = Outcome(status="needs_sign")
    with pytest.raises(ValueError):
        assign_category(pending, 1)
    with pytest.raises(ValueError):
        skip_row(pending)


@pytest.mark.parametrize("data", [
    {"status": "categorised", "is_debit": True},                    # no category
    {"status": "skipped", "is_debit": True, "category_id": 1},       # category on skipped
    {"status": "needs_sign", "is_debit": True},
    {"status": "uncategorised"},                                     # no direction
    {"status": "unknown", "is_debit": True},
])
def test_invalid_outcome_rejected(data):
    with pytest.raises(ValidationError):
        Outcome.model_validate(data)


def test_column_rule_by_header_name():
    txns = [make_txn("X")]
    sign = SIGN.model_copy(update={"column_ref": "Debit Amount"})
    assert apply_rules(txns, [sign], 7, headers={"M": "Debit Amount"})[0].is_debit is True
    assert apply_rules(txns, [sign], 7)[0].status == "needs_sign"


# ---------------------------------------------------------------- authoring helpers

def test_test_rule_on_file(env):
    draft = Rule(action="skip", operator="contains", pattern="grab")
    assert [t.description for t in test_rule(draft, env["txns"])] == [
        "GRAB*FOOD 4821", "GRAB*TRANSPORT SG", "REFUND GRAB*TRANSPORT"]
    amount = Rule(action="skip", target_field="amount", operator="amount_between", pattern="0,20")
    assert [t.description for t in test_rule(amount, env["txns"])] == [
        "GRAB*TRANSPORT SG", "NETFLIX.COM", "TOAST BOX TAMPINES", "REFUND GRAB*TRANSPORT"]


def test_conflicts_on_seed_rules(env):
    found = conflicts(env["rules"], env["txns"], profile_id=env["profile"].id)
    assert [(t.description, [r.pattern for r in rules]) for t, rules in found] == [
        ("GRAB*FOOD 4821", ["grab*food", "grab"])]


def test_conflicts_ignore_disabled_and_other_actions():
    txns = [make_txn("COFFEE")]
    rules = [cat_rule(2, 100, "coffee"), cat_rule(3, 200, "coffee", enabled=False),
             Rule(id=4, action="skip", operator="contains", pattern="coffee")]
    assert conflicts(rules, txns) == []


@pytest.mark.parametrize("description, expected", [
    ("TOAST BOX TAMPINES", "toast"),
    ("GRAB*FOOD 4821", "grab*food"),
    ("NETFLIX.COM", "netflix.com"),
    ("POS SGP NTUC FAIRPRICE", "ntuc"),
    ("Singapore Pte. Ltd. Koufu", "koufu"),
    ("(STARBUCKS) 123", "starbucks"),
    ("12 AB 3456", None),
    ("IBG POS SGP", None),
    ("", None),
])
def test_suggest_pattern(description, expected):
    assert suggest_pattern(description) == expected


def test_suggest_rule_none_without_pattern():
    assert suggest_rule(make_txn("12 AB"), 1) is None
