"""Rule engine (DESIGN.md section 7). Pure: no database or UI access.

Every transaction first gets a direction from the profile's sign rules, then
exactly one outcome, in this order:

1. decided_by = 'user' (or marked duplicate) -> kept as is
2. sign rules (this profile)  -> no match: needs_sign
3. review rules               -> uncategorised with review_reason
4. skip rules                 -> skipped
5. income                     -> skipped (decided_by 'default')
6. category rules (expenses)  -> categorised
7. no match                   -> uncategorised (decided_by 'default')

Within an action, rules run by ascending priority, ties broken by rule id; the
first match wins. Review, skip and category rules apply when global
(profile_id NULL) or scoped to the file's profile.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import lru_cache

from core.columns import resolve_column
from core.models import Outcome, Rule, Txn, parse_amount_range

DEFAULT_REVIEW_REASON = "Needs review"
GENERIC_WORDS = frozenset({"pos", "ibg", "sgp", "singapore", "pte", "ltd"})


# ---------------------------------------------------------------- matching

def _collapse(text: str) -> str:
    return " ".join(text.split())


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _target_text(rule: Rule, txn: Txn, headers: dict[str, str] | None) -> str | None:
    if rule.target_field == "description":
        return txn.description
    try:
        letter = resolve_column(rule.column_ref, headers)
    except LookupError:
        return None  # the rule names a header this file doesn't have
    return txn.raw_row.get(letter, "")


def matches(rule: Rule, txn: Txn, headers: dict[str, str] | None = None) -> bool:
    """Whether the rule's condition holds for the transaction.

    Text comparisons are case-insensitive on whitespace-collapsed text. `headers`
    (letter -> header text) lets column rules name a column by its header.
    """
    op = rule.operator
    if op == "is_negative":
        return txn.signed_amount_cents < 0
    if op == "is_positive":
        return txn.signed_amount_cents > 0
    if op == "amount_between":
        low, high = parse_amount_range(rule.pattern)
        return low * 100 <= txn.amount_cents <= high * 100

    text = _target_text(rule, txn, headers)
    if text is None:
        return False
    if op == "not_empty":
        return text.strip() != ""
    text = _collapse(text)
    if op == "regex":
        return _compiled(rule.pattern).search(text) is not None
    haystack, needle = text.casefold(), _collapse(rule.pattern).casefold()
    if op == "contains":
        return needle in haystack
    if op == "starts_with":
        return haystack.startswith(needle)
    if op == "equals":
        return haystack == needle
    raise ValueError(f"unknown operator {op!r}")


def ordered(rules: Iterable[Rule], action: str, profile_id: int | None = None) -> list[Rule]:
    """Enabled rules of one action in evaluation order: ascending priority, then id.

    With profile_id, keeps rules that are global or scoped to that profile; sign
    rules must belong to the profile itself.
    """
    def in_scope(r: Rule) -> bool:
        if profile_id is None:
            return True
        if action == "sign":
            return r.profile_id == profile_id
        return r.profile_id is None or r.profile_id == profile_id

    picked = [r for r in rules if r.action == action and r.enabled and in_scope(r)]
    return sorted(picked, key=lambda r: (r.priority, r.id if r.id is not None else 0))


def _first_match(rules: list[Rule], txn: Txn, headers: dict[str, str] | None) -> Rule | None:
    return next((r for r in rules if matches(r, txn, headers)), None)


def sign_of(txn: Txn, sign_rules: Iterable[Rule], headers: dict[str, str] | None = None) -> bool | None:
    """True = expense, False = income, None = no sign rule matched."""
    rule = _first_match(ordered(sign_rules, "sign"), txn, headers)
    return None if rule is None else rule.sign_result == "expense"


# ---------------------------------------------------------------- engine

class _RuleSet:
    """Rules split by action and pre-sorted once for a run over many rows."""

    def __init__(self, rules: Iterable[Rule], profile_id: int):
        rules = list(rules)
        self.sign = ordered(rules, "sign", profile_id)
        self.review = ordered(rules, "review", profile_id)
        self.skip = ordered(rules, "skip", profile_id)
        self.categorise = ordered(rules, "categorise", profile_id)

    def outcome(self, txn: Txn, is_debit: bool | None, headers: dict[str, str] | None) -> Outcome:
        """Steps 3-7 for a row whose direction is known (or None -> needs_sign)."""
        if is_debit is None:
            return Outcome(status="needs_sign", decided_by="default")
        if rule := _first_match(self.review, txn, headers):
            return Outcome(status="uncategorised", is_debit=is_debit, matched_rule_id=rule.id,
                           review_reason=rule.note or DEFAULT_REVIEW_REASON)
        if rule := _first_match(self.skip, txn, headers):
            return Outcome(status="skipped", is_debit=is_debit, matched_rule_id=rule.id)
        if not is_debit:
            return Outcome(status="skipped", is_debit=False, decided_by="default")
        if rule := _first_match(self.categorise, txn, headers):
            return Outcome(status="categorised", is_debit=True, category_id=rule.category_id,
                           matched_rule_id=rule.id)
        return Outcome(status="uncategorised", is_debit=True, decided_by="default")


def _kept(previous: Outcome | None) -> bool:
    return previous is not None and (previous.decided_by == "user" or previous.status == "duplicate")


def apply_rules(
    txns: Sequence[Txn],
    rules: Iterable[Rule],
    profile_id: int,
    *,
    previous: Sequence[Outcome | None] | None = None,
    headers: dict[str, str] | None = None,
) -> list[Outcome]:
    """Decide every transaction of one file. Pure: no database access.

    `previous` (aligned with txns) carries outcomes from an earlier run; user
    decisions and duplicates in it are kept. `headers` resolves column rules that
    name a column by header text.
    """
    if previous is not None and len(previous) != len(txns):
        raise ValueError(f"previous has {len(previous)} outcomes for {len(txns)} transactions")
    ruleset = _RuleSet(rules, profile_id)
    outcomes = []
    for i, txn in enumerate(txns):
        prev = previous[i] if previous is not None else None
        if _kept(prev):
            outcomes.append(prev)
            continue
        sign_rule = _first_match(ruleset.sign, txn, headers)
        is_debit = None if sign_rule is None else sign_rule.sign_result == "expense"
        outcomes.append(ruleset.outcome(txn, is_debit, headers))
    return outcomes


# ---------------------------------------------------------------- user decisions

def assign_category(outcome: Outcome, category_id: int) -> Outcome:
    """User picks a category ('assign once', or Keep on a review row).

    A kept income row (e.g. a refund) later subtracts from that category's total.
    """
    if outcome.is_debit is None:
        raise ValueError("choose Expense or Income before assigning a category")
    return outcome.model_copy(update={
        "status": "categorised", "category_id": category_id,
        "matched_rule_id": None, "decided_by": "user",
    })


def skip_row(outcome: Outcome) -> Outcome:
    """User skips a row (Skip on a review row, or any row)."""
    if outcome.is_debit is None:
        raise ValueError("choose Expense or Income before skipping")
    return outcome.model_copy(update={
        "status": "skipped", "category_id": None, "matched_rule_id": None, "decided_by": "user",
    })


def choose_sign(
    txn: Txn, is_debit: bool, rules: Iterable[Rule], profile_id: int,
    headers: dict[str, str] | None = None,
) -> Outcome:
    """User picks Expense/Income on a needs_sign row; the remaining steps run once.

    The result is a user decision, so it survives re-runs. To have rules keep
    deciding the row, save the choice as a sign rule instead.
    """
    outcome = _RuleSet(rules, profile_id).outcome(txn, is_debit, headers)
    return outcome.model_copy(update={"decided_by": "user"})


# ---------------------------------------------------------------- rule-authoring helpers

def test_rule(rule: Rule, txns: Iterable[Txn], headers: dict[str, str] | None = None) -> list[Txn]:
    """Rows a draft rule's condition matches ('Test on this file')."""
    return [t for t in txns if matches(rule, t, headers)]


test_rule.__test__ = False  # not a pytest test despite the name


def conflicts(
    rules: Iterable[Rule], txns: Iterable[Txn], *,
    profile_id: int | None = None, headers: dict[str, str] | None = None,
) -> list[tuple[Txn, list[Rule]]]:
    """Rows matched by more than one enabled category rule, with those rules in priority order."""
    category_rules = ordered(rules, "categorise", profile_id)
    found = []
    for txn in txns:
        hits = [r for r in category_rules if matches(r, txn, headers)]
        if len(hits) > 1:
            found.append((txn, hits))
    return found


def suggest_pattern(description: str) -> str | None:
    """First word with at least 3 letters that isn't generic (pos, ibg, sgp...), lower-cased.

    Words are split on whitespace with surrounding punctuation trimmed, so
    'GRAB*FOOD 4821' -> 'grab*food' and 'NETFLIX.COM' -> 'netflix.com'.
    """
    for token in description.split():
        word = token.strip("()[]{}<>.,;:!?'\"*#-_/\\").lower()
        letters = "".join(ch for ch in word if ch.isalpha())
        if len(letters) >= 3 and letters not in GENERIC_WORDS:
            return word
    return None


_OPERATOR_TEXT = {
    "contains": "contains", "starts_with": "starts with", "equals": "equals", "regex": "matches regex",
    "not_empty": "is not empty", "is_negative": "is negative", "is_positive": "is positive",
}


def describe_condition(rule: Rule, headers: dict[str, str] | None = None) -> str:
    """Plain-text condition, e.g. "description contains 'grab'" or "column M (Debit Amount) is not empty"."""
    if rule.target_field == "column":
        try:
            letter = resolve_column(rule.column_ref, headers)
            header = (headers or {}).get(letter)
            subject = f"column {letter} ({header})" if header else f"column {letter}"
        except LookupError:
            subject = f"column '{rule.column_ref}'"
    else:
        subject = rule.target_field
    if rule.operator == "amount_between":
        low, high = parse_amount_range(rule.pattern)
        return f"amount between {low} and {high}"
    text = f"{subject} {_OPERATOR_TEXT[rule.operator]}"
    return f"{text} '{rule.pattern}'" if rule.pattern else text


def describe_result(rule: Rule, category_names: dict[int, str] | None = None) -> str:
    """Plain-text outcome, e.g. 'Food', 'Skip', 'Review: Possible refund', 'Sign: expense'."""
    if rule.action == "categorise":
        return (category_names or {}).get(rule.category_id, f"category #{rule.category_id}")
    if rule.action == "skip":
        return "Skip"
    if rule.action == "review":
        return f"Review: {rule.note or DEFAULT_REVIEW_REASON}"
    return f"Sign: {rule.sign_result}"


def suggest_rule(txn: Txn, category_id: int, profile_id: int | None = None) -> Rule | None:
    """Draft categorise rule for 'Make rule' on an unmatched row; the user can edit it."""
    pattern = suggest_pattern(txn.description)
    if pattern is None:
        return None
    return Rule(action="categorise", category_id=category_id, operator="contains",
                pattern=pattern, priority=20, profile_id=profile_id)
