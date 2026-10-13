"""Load config/seed.yaml into the database. Safe to re-run: everything is upserted."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python scripts/seed.py`

import yaml  # noqa: E402
from pydantic import BaseModel, ValidationError  # noqa: E402

from core.models import Category, Profile, Rule  # noqa: E402
from db import repo  # noqa: E402
from db.connection import PROJECT_ROOT, db_path, get_conn  # noqa: E402

SEED_PATH = PROJECT_ROOT / "config" / "seed.yaml"
_SECTIONS = {"categories", "profiles", "rules"}


def _validate(model: type[BaseModel], item: dict, where: str):
    try:
        return model.model_validate(item)
    except ValidationError as exc:
        raise ValueError(f"seed {where}: {exc}") from exc


def _lookup_id(found, kind: str, name: str, where: str) -> int:
    if found is None:
        raise ValueError(f"seed {where}: unknown {kind} {name!r}")
    return found.id


def seed(conn: sqlite3.Connection, path: str | Path = SEED_PATH) -> dict[str, int]:
    """Upsert the seed file's categories, profiles (with sign rules) and rules; return counts."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if unknown := set(data) - _SECTIONS:
        raise ValueError(f"seed: unknown sections {sorted(unknown)}; expected {sorted(_SECTIONS)}")
    counts = dict.fromkeys(("categories", "profiles", "rules"), 0)

    for i, item in enumerate(data.get("categories") or []):
        repo.upsert_category(conn, _validate(Category, item, f"categories[{i}]"))
        counts["categories"] += 1

    for i, item in enumerate(data.get("profiles") or []):
        item = dict(item)
        sign_rules = item.pop("sign_rules", None) or []
        profile_id = repo.upsert_profile(conn, _validate(Profile, item, f"profiles[{i}]"))
        counts["profiles"] += 1
        for j, rule in enumerate(sign_rules):
            where = f"profiles[{i}].sign_rules[{j}]"
            repo.upsert_rule(conn, _validate(Rule, {**rule, "action": "sign", "profile_id": profile_id}, where))
            counts["rules"] += 1

    for i, item in enumerate(data.get("rules") or []):
        item, where = dict(item), f"rules[{i}]"
        if (name := item.pop("category", None)) is not None:
            item["category_id"] = _lookup_id(repo.get_category_by_name(conn, name), "category", name, where)
        if (name := item.pop("profile", None)) is not None:
            item["profile_id"] = _lookup_id(repo.get_profile_by_name(conn, name), "profile", name, where)
        repo.upsert_rule(conn, _validate(Rule, item, where))
        counts["rules"] += 1

    return counts


def main() -> None:
    conn = get_conn()
    try:
        counts = seed(conn)
    finally:
        conn.close()
    print(f"Seeded {db_path()}: {counts['categories']} categories, "
          f"{counts['profiles']} profiles, {counts['rules']} rules")


if __name__ == "__main__":
    main()
