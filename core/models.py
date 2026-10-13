"""Pydantic models: configuration (Category, Profile, Rule) and the canonical Txn.

Validation mirrors the CHECK constraints in db/schema.sql and adds the checks
SQLite can't do (regex compiles, operator fits its target field), so a bad
config fails loudly before it reaches the database. Outcome is what the rule
engine decides for one transaction.
"""
from __future__ import annotations

import codecs
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FileType = Literal["csv", "xlsx", "pdf"]
Action = Literal["sign", "review", "skip", "categorise"]
SignResult = Literal["expense", "income"]
TargetField = Literal["description", "amount", "column"]
Operator = Literal[
    "contains", "starts_with", "equals", "regex",
    "amount_between", "is_negative", "is_positive", "not_empty",
]

Status = Literal["categorised", "skipped", "uncategorised", "needs_sign", "duplicate"]
DecidedBy = Literal["rule", "user", "default"]

TEXT_OPERATORS = frozenset({"contains", "starts_with", "equals", "regex"})
AMOUNT_OPERATORS = frozenset({"amount_between", "is_negative", "is_positive"})
PATTERN_OPERATORS = TEXT_OPERATORS | {"amount_between"}


def _check_regex(value: str, what: str) -> None:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"invalid {what} regex {value!r}: {exc}") from exc


def parse_amount_range(pattern: str) -> tuple[Decimal, Decimal]:
    """Parse an amount_between pattern like '10,50' into (low, high)."""
    parts = [p.strip() for p in pattern.split(",")]
    if len(parts) != 2:
        raise ValueError(f"amount_between pattern must be 'low,high', got {pattern!r}")
    try:
        low, high = Decimal(parts[0]), Decimal(parts[1])
    except InvalidOperation as exc:
        raise ValueError(f"amount_between bounds must be numbers, got {pattern!r}") from exc
    if low < 0 or low > high:
        raise ValueError(f"amount_between needs 0 <= low <= high, got {pattern!r}")
    return low, high


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Category(_Model):
    id: int | None = None
    name: str = Field(min_length=1)
    color: str = Field(default="#9AA3AE", pattern=r"^#[0-9A-Fa-f]{6}$")
    sort_order: int = 0
    is_active: bool = True


class ColumnMap(_Model):
    """Maps canonical fields to Excel column letters ('L') or exact header names."""

    date: str = Field(min_length=1)
    description: str = Field(min_length=1)
    amount: str | None = None
    debit: str | None = None
    credit: str | None = None
    extra_description: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_amount_source(self) -> ColumnMap:
        has_split = self.debit is not None or self.credit is not None
        if self.amount is None and not has_split:
            raise ValueError("column_map needs 'amount', or 'debit' and/or 'credit'")
        if self.amount is not None and has_split:
            raise ValueError("column_map uses either 'amount' or 'debit'/'credit', not both")
        return self


class Profile(_Model):
    id: int | None = None
    name: str = Field(min_length=1)
    file_type: FileType
    filename_pattern: str | None = None
    header_row: int = Field(default=1, ge=1)
    header_signature: dict[str, str] | None = None
    skip_footer_rows: int = Field(default=0, ge=0)
    sheet_name: str | None = None
    column_map: ColumnMap
    date_format: str = Field(min_length=2)
    encoding: str = "utf-8-sig"

    @field_validator("filename_pattern")
    @classmethod
    def _filename_pattern_compiles(cls, value: str | None) -> str | None:
        if value is not None:
            _check_regex(value, "filename_pattern")
        return value

    @field_validator("date_format")
    @classmethod
    def _date_format_has_directive(cls, value: str) -> str:
        if "%" not in value:
            raise ValueError(f"date_format must be a strptime format like '%d %b %Y', got {value!r}")
        return value

    @field_validator("encoding")
    @classmethod
    def _encoding_exists(cls, value: str) -> str:
        try:
            codecs.lookup(value)
        except LookupError as exc:
            raise ValueError(f"unknown encoding {value!r}") from exc
        return value


class Rule(_Model):
    id: int | None = None
    action: Action
    sign_result: SignResult | None = None
    category_id: int | None = None
    target_field: TargetField = "description"
    column_ref: str | None = None
    operator: Operator
    pattern: str | None = None
    profile_id: int | None = None
    priority: int = Field(default=100, ge=0)
    enabled: bool = True
    note: str | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> Rule:
        if self.action == "sign":
            if self.sign_result is None:
                raise ValueError("sign rules need sign_result ('expense' or 'income')")
            if self.profile_id is None:
                raise ValueError("sign rules must belong to a profile (profile_id)")
        elif self.sign_result is not None:
            raise ValueError("sign_result is only allowed on sign rules")

        if self.action == "categorise":
            if self.category_id is None:
                raise ValueError("categorise rules need a category_id")
        elif self.category_id is not None:
            raise ValueError("category_id is only allowed on categorise rules")

        if self.target_field == "column":
            if not self.column_ref:
                raise ValueError("target_field 'column' needs a column_ref (letter or header)")
        elif self.column_ref is not None:
            raise ValueError("column_ref is only allowed when target_field is 'column'")

        op = self.operator
        if op in AMOUNT_OPERATORS and self.target_field != "amount":
            raise ValueError(f"operator {op!r} needs target_field 'amount'")
        if op in TEXT_OPERATORS and self.target_field == "amount":
            raise ValueError(f"operator {op!r} works on description or a column, not amount")
        if op == "not_empty" and self.target_field != "column":
            raise ValueError("operator 'not_empty' needs target_field 'column'")

        if op in PATTERN_OPERATORS:
            if not self.pattern:
                raise ValueError(f"operator {op!r} needs a pattern")
            if op == "regex":
                _check_regex(self.pattern, "rule")
            if op == "amount_between":
                parse_amount_range(self.pattern)
        elif self.pattern:
            raise ValueError(f"operator {op!r} takes no pattern")
        return self


class Txn(_Model):
    """Canonical transaction produced by normalise() (DESIGN.md section 6)."""

    txn_date: date
    description: str
    signed_amount_cents: int  # as the file shows it, e.g. -18.40 -> -1840
    amount_cents: int = Field(ge=0)  # abs(signed_amount_cents)
    is_debit: bool | None = None  # set by sign rules; None = no sign rule matched
    row_hash: str
    raw_row: dict[str, str]  # original cells keyed by column letter

    @model_validator(mode="after")
    def _check_amounts(self) -> Txn:
        if self.amount_cents != abs(self.signed_amount_cents):
            raise ValueError("amount_cents must equal abs(signed_amount_cents)")
        return self


class Outcome(_Model):
    """The rule engine's decision for one transaction (columns of the transactions table)."""

    status: Status
    is_debit: bool | None = None  # True expense, False income, None = needs_sign
    category_id: int | None = None
    matched_rule_id: int | None = None
    review_reason: str | None = None  # set by a review rule, e.g. 'Possible refund'
    decided_by: DecidedBy = "rule"

    @property
    def needs_decision(self) -> bool:
        """Pending: flagged by a review rule, or no direction yet."""
        return self.status == "needs_sign" or (self.status == "uncategorised" and self.review_reason is not None)

    @property
    def counts_toward_total(self) -> bool:
        """Same filter as the consolidated query: categorised rows, and unmatched expenses."""
        return self.status == "categorised" or (self.status == "uncategorised" and self.is_debit is True)

    @model_validator(mode="after")
    def _check_consistency(self) -> Outcome:
        if (self.status == "categorised") != (self.category_id is not None):
            raise ValueError("category_id is required for, and only allowed on, categorised rows")
        if self.status == "needs_sign" and self.is_debit is not None:
            raise ValueError("needs_sign rows have no direction (is_debit must be None)")
        if self.status in ("categorised", "skipped", "uncategorised") and self.is_debit is None:
            raise ValueError(f"{self.status} rows need a direction (is_debit)")
        return self


class StoredTxn(_Model):
    """A transaction row as saved in SQLite: the canonical Txn plus its current Outcome."""

    id: int
    import_id: int
    txn: Txn
    outcome: Outcome

    @property
    def signed_total_cents(self) -> int:
        """Contribution to totals: expenses add, kept income subtracts, others 0."""
        if not self.outcome.counts_toward_total:
            return 0
        return self.txn.amount_cents if self.outcome.is_debit else -self.txn.amount_cents
