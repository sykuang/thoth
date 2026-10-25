"""Query bounds must not veto parseable dates reported by a bank."""
from datetime import date

import pytest


@pytest.mark.parametrize("bank", ["ubot", "sinopac", "taishin", "rakuten", "fubon"])
@pytest.mark.parametrize("reported", ["2024/01/01", "2035/01/01"])
def test_bank_preserves_reported_dates(bank, reported, tmp_path, monkeypatch):
    if bank == "ubot":
        from backend.banks.ubot import UbotCrawler
        from tests.test_ubot_history_sync import ACCOUNT, _history_hit, _row

        row = {**_row(), "TraDate": reported, "AccountDate": "2036/01/01"}
        result = UbotCrawler._validate_history_hit(
            _history_hit(rows=[row]), identity=ACCOUNT,
            start=date(2026, 7, 1), end=date(2026, 7, 31), after_sequence=1,
        )
        assert result["records"] == [row]
    elif bank == "sinopac":
        from backend.banks.sinopac import SinopacCrawler
        from tests.test_sinopac_history_sync import _row

        row = {**_row(when=f"{reported}<br />12:34"), "DataText2": "2036/01/01"}
        SinopacCrawler._validate_history_row(
            row, currency="TWD", start=date(2026, 8, 1), end=date(2026, 8, 31),
        )
        assert row["DataText1"] == f"{reported}<br />12:34"
        assert row["DataText2"] == "2036/01/01"
    elif bank == "taishin":
        from backend.banks.taishin import TaishinCrawler
        from tests.test_taishin_history_sync import ACCOUNT, _result_snapshot

        snapshot = _result_snapshot()
        snapshot["rows"][0][:2] = [f"{reported} 12:34:56", "2036/01/01"]
        result = TaishinCrawler._validate_history_snapshot(
            snapshot, identity=ACCOUNT, period="12_months",
            start=date(2025, 9, 2), end=date(2026, 9, 2), api_row_count=1,
        )
        assert result["rows"][0]["datetime"] == f"{reported.replace('/', '-')} 12:34:56"
        assert result["rows"][0]["account_date"] == "2036-01-01"
    elif bank == "rakuten":
        from backend.banks.rakuten import RakutenCrawler
        from tests.test_rakuten_history_sync import _history_result

        result = _history_result()
        result["txDetails"][0]["sysDate"] = reported
        assert RakutenCrawler._validated_history_result(result) == result["receipt"]
        assert result["txDetails"][0]["sysDate"] == reported
    else:
        from backend.core.persist.fubon import persist_fubon
        from backend.core.store import BankStore
        from tests.test_fubon_history_sync import _payload, _result

        monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
        payload = _payload()
        payload["deposit_txn_results"][0] = _result(
            "2025-08-30", "2026-02-27", reported.replace("/", "-"),
        )
        store = BankStore("fubon")
        try:
            delta = persist_fubon(payload, store)
            assert delta["twd_txn_new"] == 2
            row = store.conn.execute(
                "SELECT txn_datetime, account_date FROM twd_transactions ORDER BY id LIMIT 1"
            ).fetchone()
            assert row is not None
            assert tuple(row) == (f"{reported.replace('/', '-')} 12:00:00", reported.replace("/", "-"))
        finally:
            store.close()


def test_fubon_future_row_does_not_advance_query_coverage(tmp_path, monkeypatch):
    from backend.core.persist import persist_collected
    from backend.core.store import BankStore
    from tests.test_fubon_history_sync import ACCOUNT, _payload, _result

    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    payload = _payload()
    payload["deposit_txn_results"][0] = _result("2025-08-30", "2026-02-27", "2035-01-01")
    store = BankStore("fubon", user_id=1, source_account_id=7)
    try:
        assert persist_collected("fubon", payload, store)["twd_txn_new"] == 2
        assert store.latest_twd_transaction_dates() == {ACCOUNT: date(2026, 8, 30)}
        assert store.conn.execute(
            "SELECT account_date FROM twd_transactions WHERE account_date = '2035-01-01'"
        ).fetchone() is not None
    finally:
        store.close()


@pytest.mark.parametrize("bank", ["ubot", "sinopac"])
@pytest.mark.parametrize("reported", ["2024/01/01", "2035/01/01"])
def test_deposit_persistence_preserves_dates_and_query_cursor(bank, reported, tmp_path, monkeypatch):
    from backend.core.persist import persist_collected
    from backend.core.store import BankStore

    if bank == "ubot":
        from tests.test_ubot_history_sync import _persist_payload
        payload = _persist_payload()
        row = payload["twd_txns"][0]["NTDetailList"][0]
        row.update(TraDate=reported, AccountDate="2036/01/01")
    else:
        from tests.test_sinopac_history_sync import _persist_payload
        payload = _persist_payload()
        row = payload["account_transactions"][-1]["records"][0]
        row.update(DataText1=f"{reported}<br />12:34", DataText2="2036/01/01")
    monkeypatch.setattr(
        f"backend.core.persist.{bank}._today",
        lambda: date.fromisoformat(payload["history_coverage"]["as_of"]),
    )
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore(bank, user_id=1, source_account_id=7)
    try:
        assert persist_collected(bank, payload, store)["twd_txn_new"] == 1
        saved = store.conn.execute("SELECT txn_datetime, account_date FROM twd_transactions").fetchone()
        assert saved is not None
        assert saved["txn_datetime"][:10] == reported.replace("/", "-")
        assert saved["account_date"] == "2036-01-01"
        domain = payload["history_coverage"]["domains"][0]
        expected = domain["expected"][0]
        assert store._transaction_cursor_dates(domain["domain"]) == {
            expected["identity"]: date.fromisoformat(expected["end"]),
        }
    finally:
        store.close()


@pytest.mark.parametrize("reported", ["2024/01/01", "2035/01/01"])
def test_loan_preserves_outside_window_payment_dates_on_replay(tmp_path, monkeypatch, reported):
    from backend.core.persist.sinopac import _persist_sinopac as persist_sinopac
    from backend.core.store import BankStore
    from tests.test_sinopac_loan import _loan_with_repayments

    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    payload = _loan_with_repayments()
    payload["loan"]["details"][0]["repayments"][0]["records"][0]["DataValue2"] = reported
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        for _ in range(2):
            assert persist_sinopac(payload, store)["loan_repayments"] == 1
        saved = store.conn.execute("SELECT paid_on FROM loan_repayments").fetchall()
        assert [row[0] for row in saved] == [reported.replace("/", "-")]
    finally:
        store.close()


@pytest.mark.parametrize("left,right", [(1, 1), (2, 1), (1, 2)])
@pytest.mark.parametrize("shared", [True, False])
def test_fubon_shared_observations_preserve_maximum_multiplicity(tmp_path, monkeypatch, left, right, shared):
    from backend.core.persist import persist_collected
    from backend.core.store import BankStore
    from tests.test_fubon_history_sync import _payload, _result

    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    payload = _payload()
    payload["deposit_txn_results"] = [
        _result("2025-08-30", "2026-02-27", "2026-02-28"),
        _result("2026-02-28", "2026-08-30", "2026-02-28"),
    ]
    if not shared:
        payload["deposit_txn_results"][1]["snapshot"]["gridRows"][0][2] = "其他利息"
    expected = max(left, right) if shared else left + right
    for result, count in zip(payload["deposit_txn_results"], (left, right), strict=True):
        snapshot = result["snapshot"]
        snapshot["gridRows"] *= count
        snapshot.update(gridRowCount=count, rawDataRowCount=count, totalCount=count)
        snapshot["gridText"] = "\n".join("\t".join(row) for row in snapshot["gridRows"])
    store = BankStore("fubon", user_id=1, source_account_id=7)
    try:
        assert persist_collected("fubon", payload, store)["twd_txn_new"] == expected
        assert persist_collected("fubon", payload, store)["twd_txn_new"] == 0
        saved = store.conn.execute("SELECT COUNT(*), SUM(income) FROM twd_transactions").fetchone()
        assert saved is not None
        assert tuple(saved) == (expected, 5 * expected)
    finally:
        store.close()


def test_cathay_native_future_posting_preserves_data_not_false_card_coverage(tmp_path, monkeypatch):
    from backend.banks.cathay import CathayCrawler
    from backend.core.base import ResponseCollector
    from backend.core.persist import persist_collected
    from backend.core.store import BankStore
    from tests.test_cathay_history_sync import _FetchPage, _template

    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    crawler = object.__new__(CathayCrawler)
    crawler.transaction_cursors = {}
    monkeypatch.setattr(crawler, "_seed_twd_query_templates", lambda *_: [_template("00001234")])
    result = crawler._collect_twd_history(_FetchPage(empty=True), ResponseCollector(), as_of=date(2026, 8, 30))
    row = crawler._norm_card_txn(dict(cardNo="9000000000000001", consumeDate="2026-08-01",
                                    postingDate="2035-01-01", transDesc="synthetic snapshot",
                                    amount=5, currency="TWD"))
    payload = dict(twd_transactions=result["accounts"], history_coverage=result["coverage"],
                   credit_card={"billed_detail": {"TWD": [row]}})
    store = BankStore("cathay", user_id=1, source_account_id=7)
    try:
        assert persist_collected("cathay", payload, store)["card_billed_new"] == 1
        assert store.latest_card_transaction_dates() == {}
        assert store.conn.execute("SELECT post_date FROM card_billed_txns").fetchone()[0] == "2035-01-01"
    finally:
        store.close()


def test_fubon_keeps_boundary_row_when_neighbor_window_is_empty(tmp_path, monkeypatch):
    from backend.core.persist.fubon import persist_fubon
    from backend.core.store import BankStore
    from tests.test_fubon_history_sync import _payload, _result

    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    payload = _payload()
    payload["deposit_txn_results"] = [
        _result("2025-08-30", "2026-02-27", "2026-02-28"),
        _result("2026-02-28", "2026-08-30", "2026-08-29", empty=True),
    ]
    payload["history_coverage"]["domains"][0]["windows"][1]["status"] = "explicit_empty"
    store = BankStore("fubon")
    try:
        assert persist_fubon(payload, store)["twd_txn_new"] == 1
        assert store.conn.execute("SELECT account_date FROM twd_transactions").fetchone()[0] == "2026-02-28"
    finally:
        store.close()
