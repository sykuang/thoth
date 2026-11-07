"""Synthetic observed-shape controls; no real financial identifiers or bank IO."""
from datetime import date

import pytest
from backend.core.persist.ubot import _persist_ubot
from backend.core.store import BankStore


PLAIN = "SYNTHETIC TAX  1200 USD 05/12"
CONTROL = "SYNTHETIC TAX\x1f 1200\x1eUSD\x1f05/12"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    value = BankStore("ubot", user_id=7, source_account_id=91)
    try:
        yield value
    finally:
        value.close()


def row(desc=PLAIN, original=0, cycle=None, posting="2026-10-11"):
    return {"card_no": None, "date": "2026-05-05", "post_date": posting,
            "desc": desc, "amount": 100, "currency": "TWD",
            "consume_currency": "TWD", "consume_amount": original,
            "txn_type": "spending", "bill_date": cycle}


def rows(store):
    return [dict(r) for r in store.conn.execute("SELECT * FROM card_billed_txns ORDER BY id")]


def test_unchanged_replay_is_idempotent(store):
    store.upsert_card_billed([row()])
    store.upsert_card_billed([row()])
    assert len(rows(store)) == 1


def test_different_installments_are_not_duplicates(store):
    for i in range(3):
        store.upsert_card_billed([row(desc=f"SYNTHETIC TAX {i+1:02d}/12", posting=f"2026-{7+i:02d}-11")])
    assert len(rows(store)) == 3


def test_original_zero_to_missing_should_not_fork_observed_local_tax(store):
    store.upsert_card_billed([row()])
    store.upsert_card_billed([row(original=None, cycle="2026-11-03")])
    assert len(rows(store)) == 1, "Observed zero-to-missing shape forks the saved row"


def test_control_character_description_should_not_fork_observed_tax(store):
    store.upsert_card_billed([row()])
    store.upsert_card_billed([row(desc=CONTROL)])
    assert len(rows(store)) == 1, "Observed description control bytes fork the saved row"


def test_observed_three_representations_should_be_one_occurrence(store):
    for item in (row(), row(desc=CONTROL), row(original=None, cycle="2026-11-03")):
        store.upsert_card_billed([item])
    assert len(rows(store)) == 1, "Observed three representations produce three persisted rows"


def test_actual_ubot_mapping_pending_zero_then_statement_blank(store):
    unbilled = {"cardNo": None, "effectiveDate": "2026/05/05", "postDate": "2026/10/11",
                "txDesc": PLAIN, "txAmt": "100", "oriAmt": "0", "Currency": "TWD", "txCode": "40"}
    _persist_ubot({"card_unbilled": {"CardList": [unbilled]}}, store, as_of=date(2026, 10, 25))
    first = rows(store)
    assert len(first) == 1 and first[0]["consume_amount"] == 0 and first[0]["bill_date"] is None
    billed = {**unbilled, "effectDate": "2026/05/05", "oriAmt": ""}
    _persist_ubot({"card_billed": [{"CardHeader": {"stmtDate": "2026/11/03"}, "CardList": [billed]}],
                   "card_unbilled": {"CardList": []}}, store, as_of=date(2026, 11, 4))
    assert len(rows(store)) == 1, "Actual UBOT producer mapping plus Store duplicates the observed local tax shape"
