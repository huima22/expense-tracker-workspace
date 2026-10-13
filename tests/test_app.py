"""M4: drive the Streamlit page headlessly (streamlit.testing AppTest).

AppTest can't upload files, so the DBS fixture is imported through the pipeline
first; the page then picks it up from SQLite, as it does after a page reload.
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core import pipeline
from db import repo
from db.connection import get_conn
from scripts.seed import seed
from tests.conftest import dbs_text

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    monkeypatch.setenv("EXPENSES_DB", str(path))
    conn = get_conn(path)
    seed(conn)
    yield conn
    conn.close()


@pytest.fixture
def loaded(db):
    session_id = repo.create_session(db, "October 2026")
    dbs = repo.get_profile_by_name(db, "DBS Savings CSV")
    import_id = pipeline.import_file(db, session_id, "dbs_sample.csv", dbs_text().encode(), dbs.id)
    rows = {r.txn.description.split()[0]: r.id for r in repo.list_transactions(db, import_id)}
    ids = {c.name: c.id for c in repo.list_categories(db)}
    return {"conn": db, "import_id": import_id, "rows": rows, "ids": ids}


def run(target) -> AppTest:
    """Run the app (an AppTest, or a widget after click()/set_value()) and fail on page exceptions."""
    at = target.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def page_text(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def new_app() -> AppTest:
    return AppTest.from_file(APP, default_timeout=30)


def test_empty_page_renders(db):
    at = run(new_app())
    text = page_text(at)
    assert "Upload" in text
    assert "0 files loaded" in text
    assert repo.list_sessions(db)[0]["name"]  # a '<Month YYYY>' session was created


def test_loaded_file_renders_card(loaded):
    at = run(new_app())
    text = page_text(at)
    assert "dbs_sample.csv" in text
    assert "File total" in text and "266.53" in text
    assert "1 need a decision" in text
    assert at.button(key=f"make_{loaded['rows']['TOAST']}")
    assert at.selectbox(key=f"keep_{loaded['rows']['REFUND']}")


def test_make_rule_for_toast_box_updates_total(loaded):
    """M4 acceptance: add a rule for TOAST BOX and the file total updates without errors."""
    at = run(new_app())
    run(at.button(key=f"make_{loaded['rows']['TOAST']}").click())
    assert at.text_input(key="rf_pattern").value == "toast"
    at.selectbox(key="rf_category").set_value(loaded["ids"]["Food"])
    run(at.button(key="rf_save").click())

    outcome = repo.get_transaction(loaded["conn"], loaded["rows"]["TOAST"]).outcome
    assert outcome.category_id == loaded["ids"]["Food"]
    text = page_text(at)
    assert "31.30" in text  # Food 23.50 + 7.80
    assert "266.53" in text  # file total unchanged
    assert "Uncategorised" not in text.split("Category result")[-1]


def test_test_rule_button_shows_matches(loaded):
    at = run(new_app())
    at.text_input(key="rf_pattern").set_value("grab")
    run(at.button(key="rf_test").click())
    assert any("Matches 3 of 8 rows" in i.value for i in at.info)


def test_invalid_rule_shows_error(loaded):
    at = run(new_app())
    at.text_input(key="rf_pattern").set_value("toast")
    run(at.button(key="rf_save").click())  # no category picked
    assert any("category" in e.value for e in at.error)


def test_keep_refund_from_page(loaded):
    at = run(new_app())
    run(at.selectbox(key=f"keep_{loaded['rows']['REFUND']}").set_value(loaded["ids"]["Transport"]))
    assert "254.53" in page_text(at)


def test_assign_and_undo_from_page(loaded):
    at = run(new_app())
    toast = loaded["rows"]["TOAST"]
    run(at.selectbox(key=f"assign_{toast}").set_value(loaded["ids"]["Groceries"]))
    assert repo.get_transaction(loaded["conn"], toast).outcome.decided_by == "user"
    run(at.button(key=f"reset_{toast}").click())
    assert repo.get_transaction(loaded["conn"], toast).outcome.status == "uncategorised"


def test_edit_and_disable_rule_from_page(loaded):
    conn = loaded["conn"]
    netflix = next(r for r in repo.list_rules(conn) if r.pattern == "netflix")
    at = run(new_app())
    run(at.button(key=f"rule_edit_{netflix.id}").click())
    assert at.text_input(key="rf_pattern").value == "netflix"
    at.number_input(key="rf_priority").set_value(5)
    run(at.button(key="rf_save").click())
    assert repo.get_rule(conn, netflix.id).priority == 5

    run(at.toggle(key=f"rule_on_{netflix.id}").set_value(False))
    assert repo.get_rule(conn, netflix.id).enabled is False
    netflix_row = next(r for r in repo.list_transactions(conn, loaded["import_id"])
                       if r.txn.description == "NETFLIX.COM")
    assert netflix_row.outcome.status == "uncategorised"


# ---------------------------------------------------------------- M5: several files, consolidate

@pytest.fixture
def two_files(loaded):
    """DBS committed, Citi imported as the current draft."""
    from tests.conftest import CITI_XLSX, citi_profile, citi_sign_rules
    conn = loaded["conn"]
    session_id = repo.list_sessions(conn)[0]["id"]
    pipeline.commit(conn, loaded["import_id"])
    citi_id = repo.create_profile(conn, citi_profile().model_copy(update={"id": None}))
    for rule in citi_sign_rules(citi_id):
        repo.create_rule(conn, rule.model_copy(update={"id": None}))
    citi_import = pipeline.import_file(conn, session_id, CITI_XLSX.name, CITI_XLSX.read_bytes(), citi_id)
    return {**loaded, "session_id": session_id, "citi_import": citi_import}


def test_add_new_file_commits_and_collapses(loaded):
    at = run(new_app())
    run(at.button(key="add_file").click())
    assert repo.get_import(loaded["conn"], loaded["import_id"])["status"] == "committed"
    text = page_text(at)
    assert "Needs review" in text and "266.53" in text  # collapsed one-line summary
    assert "#### Upload" in text  # empty upload card
    assert at.button(key="add_file").disabled
    assert f"open_{loaded['import_id']}" in [b.key for b in at.button]


def test_consolidate_two_files(two_files):
    at = run(new_app())
    assert at.button(key="consolidate").label == "Consolidate 2 files"
    run(at.button(key="consolidate").click())
    text = page_text(at)
    assert "Consolidated" in text
    assert "927.01" in text
    assert "1 row(s) totalling 12.00 still need a decision" in "\n".join(w.value for w in at.warning)
    assert len(at.get("download_button")) == 2
    imports = repo.list_imports(two_files["conn"], two_files["session_id"])
    assert all(i["status"] == "committed" for i in imports)


def test_consolidated_view_survives_reload_and_cards_open(two_files):
    pipeline.consolidate(two_files["conn"], two_files["session_id"])
    at = run(new_app())  # fresh browser session: no session_state
    assert "927.01" in page_text(at)
    run(at.button(key=f"open_{two_files['import_id']}").click())
    assert at.text_input(key="rf_pattern")  # the DBS card is open with its rule form
    assert at.button(key=f"make_{two_files['rows']['TOAST']}")
    run(at.button(key="collapse_card").click())
    assert f"open_{two_files['import_id']}" in [b.key for b in at.button]


def test_remove_file(loaded):
    at = run(new_app())
    run(at.button(key="remove_file").click())
    assert repo.get_import(loaded["conn"], loaded["import_id"]) is None
    assert "0 files loaded" in page_text(at)
