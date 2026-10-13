"""Rule condition matcher (used by sign rules in M2; the full engine is tested in M3)."""
from datetime import date

import pytest

from core.models import Rule, Txn
from core.rules import matches, sign_of


def txn(description="GRAB*FOOD 4821", signed=2350, raw_row=None):
    return Txn(txn_date=date(2026, 10, 3), description=description, signed_amount_cents=signed,
               amount_cents=abs(signed), row_hash="h", raw_row=raw_row or {"C": "DR", "M": "23.50", "N": ""})


def rule(**kw):
    return Rule.model_validate({"action": "skip", **kw})


@pytest.mark.parametrize("kw, expected", [
    ({"operator": "contains", "pattern": "grab*food"}, True),
    ({"operator": "contains", "pattern": "GRAB  *food"}, False),
    ({"operator": "contains", "pattern": "grab"}, True),
    ({"operator": "starts_with", "pattern": "grab"}, True),
    ({"operator": "starts_with", "pattern": "food"}, False),
    ({"operator": "equals", "pattern": "grab*food 4821"}, True),
    ({"operator": "equals", "pattern": "grab*food"}, False),
    ({"operator": "regex", "pattern": r"grab\*?food"}, True),
    ({"operator": "regex", "pattern": r"^food"}, False),
    ({"operator": "amount_between", "target_field": "amount", "pattern": "20,23.50"}, True),
    ({"operator": "amount_between", "target_field": "amount", "pattern": "0,5"}, False),
    ({"operator": "is_positive", "target_field": "amount"}, True),
    ({"operator": "is_negative", "target_field": "amount"}, False),
    ({"operator": "not_empty", "target_field": "column", "column_ref": "M"}, True),
    ({"operator": "not_empty", "target_field": "column", "column_ref": "N"}, False),
    ({"operator": "not_empty", "target_field": "column", "column_ref": "Z"}, False),
    ({"operator": "equals", "target_field": "column", "column_ref": "C", "pattern": "dr"}, True),
    ({"operator": "equals", "target_field": "column", "column_ref": "C", "pattern": "CR"}, False),
])
def test_operators(kw, expected):
    assert matches(rule(**kw), txn()) is expected


def test_whitespace_is_collapsed():
    assert matches(rule(operator="contains", pattern="sp services"), txn("SP   SERVICES\tLTD"))


def test_column_by_header_name():
    r = rule(operator="not_empty", target_field="column", column_ref="Debit Amount")
    assert matches(r, txn(), headers={"M": "Debit Amount"})
    assert not matches(r, txn(), headers={"M": "Something else"})
    assert not matches(r, txn())  # no headers known


def test_sign_of():
    expense = Rule(id=1, action="sign", sign_result="expense", target_field="amount",
                   operator="is_negative", profile_id=1, priority=1)
    income = Rule(id=2, action="sign", sign_result="income", target_field="amount",
                  operator="is_positive", profile_id=1, priority=2)
    assert sign_of(txn(signed=-1840), [income, expense]) is True
    assert sign_of(txn(signed=1840), [income, expense]) is False
    assert sign_of(txn(signed=1840), [expense]) is None
