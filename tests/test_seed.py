"""M1: seed.yaml loads the starter data and re-seeding changes nothing."""
import pytest

from db import repo
from scripts.seed import seed
from tests.conftest import dump, own_name_pattern


def test_seed_loads_categories_in_order(seeded):
    assert [c.name for c in repo.list_categories(seeded)] == [
        "Food", "Transport", "Groceries", "Subscriptions", "Utilities"]


def test_seed_loads_dbs_profile(seeded):
    profile = repo.get_profile_by_name(seeded, "DBS Savings CSV")
    assert profile.file_type == "csv"
    assert profile.header_row == 6
    assert profile.date_format == "%d %b %Y"
    assert profile.column_map.model_dump(exclude_defaults=True) == {
        "date": "A", "description": "L", "debit": "M", "credit": "N"}


def test_seed_rules_match_design_table(seeded):
    dbs = repo.get_profile_by_name(seeded, "DBS Savings CSV").id
    names = {c.id: c.name for c in repo.list_categories(seeded)}

    def describe(r):
        outcome = r.sign_result or names.get(r.category_id) or r.note
        return (r.priority, r.action, r.column_ref or r.pattern, r.operator, outcome, r.profile_id)

    assert [describe(r) for r in repo.list_rules(seeded)] == [
        (1, "sign", "M", "not_empty", "expense", dbs),
        (2, "sign", "N", "not_empty", "income", dbs),
        (1, "review", "refund", "contains", "Possible refund", None),
        (1, "skip", own_name_pattern(), "contains", "Own-account transfer", None),
        (10, "categorise", "grab*food", "contains", "Food", None),
        (20, "categorise", "grab", "contains", "Transport", None),
        (20, "categorise", "fairprice", "contains", "Groceries", None),
        (20, "categorise", "netflix", "contains", "Subscriptions", None),
        (20, "categorise", "sp services", "contains", "Utilities", None),
    ]


def test_reseed_changes_nothing(seeded):
    before = dump(seeded)
    assert seed(seeded) == {"categories": 5, "profiles": 1, "rules": 9}
    assert dump(seeded) == before


def test_seed_unknown_category_fails_loudly(conn, tmp_path):
    path = tmp_path / "seed.yaml"
    path.write_text("rules:\n  - {action: categorise, category: Nope, operator: contains, pattern: x}\n")
    with pytest.raises(ValueError, match=r"rules\[0\]: unknown category 'Nope'"):
        seed(conn, path)


def test_seed_invalid_rule_names_the_entry(conn, tmp_path):
    path = tmp_path / "seed.yaml"
    path.write_text("rules:\n  - {action: skip, operator: regex, pattern: '(('}\n")
    with pytest.raises(ValueError, match=r"rules\[0\]"):
        seed(conn, path)


def test_seed_unknown_section_rejected(conn, tmp_path):
    path = tmp_path / "seed.yaml"
    path.write_text("categoriez: []\n")
    with pytest.raises(ValueError, match="unknown sections"):
        seed(conn, path)
