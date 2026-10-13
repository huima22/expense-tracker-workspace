"""Row hashing and duplicate detection (pipeline stage 4)."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import date

from core.models import Outcome, Txn


def row_hash(profile_id: int | None, txn_date: date, signed_amount_cents: int,
             description: str, occurrence: int) -> str:
    """sha256 of profile, date, signed cents, lower-cased description and occurrence index.

    The occurrence index (0 for the first identical row in a file, 1 for the next...)
    keeps two genuine identical purchases on the same day from collapsing into one.
    """
    key = "|".join([
        str(profile_id if profile_id is not None else 0),
        txn_date.isoformat(),
        str(signed_amount_cents),
        " ".join(description.split()).lower(),
        str(occurrence),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def mark_duplicates(txns: Iterable[Txn], existing_hashes: set[str]) -> list[bool]:
    """True for each transaction whose hash already exists in an earlier committed import."""
    return [t.row_hash in existing_hashes for t in txns]


def apply_duplicates(outcomes: Sequence[Outcome], duplicate: Sequence[bool]) -> list[Outcome]:
    """Duplicates override whatever the rules decided (they're excluded from totals);
    the direction is kept for display."""
    return [Outcome(status="duplicate", is_debit=o.is_debit, decided_by="default") if dup else o
            for o, dup in zip(outcomes, duplicate)]
