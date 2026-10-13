from pathlib import Path

import pytest
import yaml

from core.models import Profile, Rule
from db import repo
from db.connection import get_conn
from scripts.seed import SEED_PATH, seed

FIXTURES = Path(__file__).parent / "fixtures"
DBS_CSV = FIXTURES / "dbs_sample.csv"
CITI_XLSX = FIXTURES / "citi_sample.xlsx"
FIXTURE_OWN_NAME = "<YOUR NAME>"  # placeholder used in dbs_sample.csv


def own_name_pattern() -> str:
    """The own-account skip rule's pattern in config/seed.yaml.

    Tests read it rather than hard-coding '<YOUR NAME>', so they keep passing
    after you put your real name in the seed file.
    """
    rules = yaml.safe_load(SEED_PATH.read_text(encoding="utf-8"))["rules"]
    return next(r["pattern"] for r in rules if r["action"] == "skip")


def dbs_text() -> str:
    """The DBS fixture with the own-name placeholder replaced by the seeded pattern."""
    return DBS_CSV.read_text(encoding="utf-8").replace(FIXTURE_OWN_NAME, own_name_pattern().upper())


@pytest.fixture
def db_file(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def conn(db_file):
    connection = get_conn(db_file)
    yield connection
    connection.close()


@pytest.fixture
def seeded(conn):
    seed(conn)
    return conn


@pytest.fixture
def dbs(seeded):
    """The seeded DBS profile and its sign rules."""
    profile = repo.get_profile_by_name(seeded, "DBS Savings CSV")
    return profile, repo.list_rules(seeded, action="sign", profile_id=profile.id)


def citi_profile(profile_id: int = 2) -> Profile:
    """A card-export profile for the Citi fixture (not part of the seed)."""
    return Profile(
        id=profile_id,
        name="Citi Card XLSX",
        file_type="xlsx",
        filename_pattern=r"(?i)citi.*\.xlsx$",
        header_row=4,
        header_signature={"A": "Transaction Date", "C": "Description"},
        skip_footer_rows=1,
        sheet_name="Transactions",
        column_map={"date": "A", "description": "C", "amount": "D"},
        date_format="%d/%m/%Y",
    )


def citi_sign_rules(profile_id: int = 2) -> list[Rule]:
    """Spend shown as positive (DESIGN.md section 7, typical card export)."""
    return [
        Rule(id=101, action="sign", sign_result="expense", target_field="amount",
             operator="is_positive", profile_id=profile_id, priority=1),
        Rule(id=102, action="sign", sign_result="income", target_field="amount",
             operator="is_negative", profile_id=profile_id, priority=2),
    ]


def dump(conn):
    """Full SQL dump, for checking that re-running init or seed changes nothing."""
    return list(conn.iterdump())
