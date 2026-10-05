from datetime import date

import pytest

from backend.banks.esun_spa.products import _statement_cycle
from backend.core.store import BankStore
from backend.server import auto_debit_payments as adp
from backend.server.auto_debit_settings_repo import AutoDebitSetting

TODAY = date(2026, 10, 5)
CYCLE = {"statement_close_date": "2026-09-14", "payment_due_date": "2026-09-29",
         "statement_amount": 5000}


def _store(tmp_path, bank):
    s = BankStore(str(tmp_path / f"{bank}.sqlite"), user_id=6)
    s.upsert_cards([{"number": "****2869", "name": "c", "type": "credit"}])
    return s


def _debit(store, day, desc, amount, account="A1"):
    store.conn.execute(
        "INSERT INTO twd_transactions (user_id, account_no, txn_datetime, description, expend,"
        " first_seen, dedup_key) VALUES (6, ?, ?, ?, ?, 'x', ?)",
        (account, f"{day}T00:00:00", desc, amount, f"{account}{day}{desc}{amount}"),
    )
    store.conn.commit()


def _card(store):
    return dict(store.conn.execute(
        "SELECT bill_due_amount, last_payment_amount, last_payment_date FROM cards").fetchone())


def test_debits_match_only_the_card_bank_on_the_configured_account(tmp_path):
    s = _store(tmp_path, "sinopac")
    _debit(s, "2026-09-29", "ACH代收 - 星展銀行", 3000)
    _debit(s, "2026-10-02", "永豐卡費 - 1234", 4000)
    _debit(s, "2026-09-07", "放款本息 - 99", 9000)
    _debit(s, "2026-09-30", "ACH代收 - 星展銀行", 1, account="OTHER")
    assert adp.card_debits(s, "A1", "dbs", TODAY) == [("2026-09-29", 3000)]
    assert adp.card_debits(s, "A1", "sinopac", TODAY) == [("2026-10-02", 4000)]
    assert adp.card_debits(s, "A1", "hsbc", TODAY) == []


@pytest.mark.parametrize("debits, remaining", [
    ([("2026-09-29", 5000)], 0),                       # paid on time
    ([("2026-10-01", 5000)], 0),                       # holiday pushed past due: still this cycle
    ([("2026-08-28", 7837), ("2026-09-29", 2000)], 3000),  # only post-close debits count
    ([("2026-08-28", 7837)], 5000),                    # last cycle's payment never settles this one
    ([("2026-09-20", 3000), ("2026-09-29", 4000)], 0),  # split payments sum; overpay floors at 0
])
def test_statement_fact_uses_debits_after_close_without_due_upper_bound(debits, remaining):
    fact = adp.statement_fact(CYCLE, debits)
    assert fact["remaining_due"] == remaining
    assert (fact["last_payment_date"], fact["last_payment_amount"]) == debits[-1]
    assert fact["status"] == ("unpaid" if remaining else "paid")


def test_no_setting_never_touches_cards(tmp_path, monkeypatch):
    s = _store(tmp_path, "esun")
    _debit(s, "2026-09-29", "玉山卡款扣繳", 5000)
    monkeypatch.setattr(adp.auto_debit_settings_repo, "list_settings", lambda uid: [])
    assert adp.apply_auto_debit_payments("esun", 6, s, {"card_statement_cycle": CYCLE}) == 0
    assert _card(s) == {"bill_due_amount": None, "last_payment_amount": None, "last_payment_date": None}


def _settings(monkeypatch, card_bank, account_bank):
    monkeypatch.setattr(adp.auto_debit_settings_repo, "list_settings", lambda uid: [
        AutoDebitSetting(card_bank, account_bank, "A1", "t")])
    monkeypatch.setattr(adp, "LOOKBACK_DAYS", 10_000)


def test_esun_same_bank_derives_remaining_and_payment(tmp_path, monkeypatch):
    s = _store(tmp_path, "esun")
    _debit(s, "2026-08-28", "玉山卡款扣繳 - 鄺", 7837)
    _debit(s, "2026-09-29", "玉山卡款扣繳", 5000)
    _settings(monkeypatch, "esun", "esun")
    assert adp.apply_auto_debit_payments("esun", 6, s, {"card_statement_cycle": CYCLE}) == 1
    assert _card(s) == {"bill_due_amount": 0, "last_payment_amount": 5000,
                        "last_payment_date": "2026-09-29"}


def test_bank_native_remaining_wins_and_only_payment_advances(tmp_path, monkeypatch):
    s = _store(tmp_path, "esun")
    s.update_card_bill_facts([{"number": "****2869", "bill_due_amount": 1234,
                               "payment_due_date": "2026-09-29"}])
    _debit(s, "2026-09-29", "玉山卡款扣繳", 5000)
    _settings(monkeypatch, "esun", "esun")
    adp.apply_auto_debit_payments("esun", 6, s, {"card_statement_cycle": CYCLE, "card_bill_facts_ok": True})
    assert _card(s) == {"bill_due_amount": 1234, "last_payment_amount": 5000,
                        "last_payment_date": "2026-09-29"}


def test_cross_bank_debit_account_sync_updates_card_bank_and_never_regresses(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    debit = BankStore("sinopac", user_id=6)
    card = BankStore("dbs", user_id=6)
    card.upsert_cards([{"number": "****1111", "name": "d", "type": "credit"}])
    card.update_card_bill_facts([{"number": "****1111", "bill_due_amount": 3000,
                                  "last_payment_amount": 9, "last_payment_date": "2026-10-04"}])
    card.close()
    _debit(debit, "2026-09-29", "ACH代收 - 星展銀行", 3000)
    _settings(monkeypatch, "dbs", "sinopac")
    adp.apply_auto_debit_payments("sinopac", 6, debit, {})
    card = BankStore("dbs", user_id=6)
    assert _card(card)["last_payment_date"] == "2026-10-04"  # older debit never regresses
    _debit(debit, "2026-10-05", "ACH代收 - 星展銀行", 3000)
    adp.apply_auto_debit_payments("sinopac", 6, debit, {})
    assert _card(card) == {"bill_due_amount": 3000, "last_payment_amount": 3000,
                           "last_payment_date": "2026-10-05"}


def test_esun_statement_cycle_parser_is_strict():
    body = {"billInfo": {"billDate": "20260914", "paymentDueDate": "20260929",
                         "billTotalInfoList": [{"billTotalCurrency": "TWD", "billTotalAmount": 5193}]}}
    assert _statement_cycle(body) == {"statement_close_date": "2026-09-14",
                                      "payment_due_date": "2026-09-29", "statement_amount": 5193}
    bad = [
        {"billInfo": {**body["billInfo"], "billDate": "20260231"}},
        {"billInfo": {**body["billInfo"], "billTotalInfoList": [{"billTotalCurrency": "USD", "billTotalAmount": 1}]}},
        {"billInfo": {**body["billInfo"], "billTotalInfoList": [{"billTotalCurrency": "TWD", "billTotalAmount": "5193"}]}},
        {"billInfo": {**body["billInfo"], "billDate": "20261001"}},
        {"billInfo": None},
    ]
    assert all(_statement_cycle(b) is None for b in bad)
