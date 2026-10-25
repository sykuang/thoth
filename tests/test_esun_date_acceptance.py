"""Bank row dates are data, not evidence of query ownership or completeness."""
from copy import deepcopy
from datetime import date

import pytest

from backend.banks.esun import EsunCrawler
from backend.banks.esun_spa.rows import normalize_row
from backend.core.persist import persist_collected
from backend.core.persist.esun import persist_esun
from backend.core.store import BankStore
from tests.test_esun_history_sync import _bound_coverage, _bound_result
from tests.test_esun_main_persistence_backport import ACCOUNT, OTHER, coverage, row


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("esun", user_id=7, source_account_id=93)
    yield store
    store.close()


@pytest.mark.parametrize("covered", [False, True])
def test_esun_suppresses_row_cursor_only_with_query_coverage(store, monkeypatch, covered):
    upsert = store.upsert_twd_txns

    def write(rows, **kwargs):
        assert kwargs.pop("suppress_history_cursor", None) is covered
        return upsert(rows, **kwargs)

    monkeypatch.setattr(store, "upsert_twd_txns", write)
    data: dict = {"twd_txn_results": [_bound_result()]}
    if covered:
        data["history_coverage"] = _bound_coverage()
    assert persist_esun(data, store)["twd_txn_new"] == 1


@pytest.mark.parametrize("cells,stamp,posting", [
    (["2024/01/01", "12:00:00", "利息", "", "2", "84"], "2024-01-01 12:00:00", "2024-01-01"),
    (["2099/01/01", "12:00:00", "利息", "", "2", "84"], "2099-01-01 12:00:00", "2099-01-01"),
    (["2026/08/20", "2024/01/01", "利息", "", "2", "84"], "2026-08-20", "2024-01-01"),
])
@pytest.mark.parametrize("collect", [False, True])
def test_legacy_outside_rows_survive_collector_and_sqlite(store, cells, stamp, posting, collect):
    result = _bound_result()
    result["snapshot"]["gridRows"] = [cells]
    result["snapshot"]["gridText"] = " ".join(cells)
    history = _bound_coverage()
    if collect:
        history["domains"][0]["windows"] = [EsunCrawler._validated_twd_history_result(
            result, identity=result["account_no"], start=date(2025, 8, 31), end=date(2026, 8, 30),
        )]
    assert persist_collected("esun", {"twd_txn_results": [result], "history_coverage": history}, store)["twd_txn_new"] == 1
    saved = store.conn.execute("SELECT txn_datetime, account_date FROM twd_transactions").fetchone()
    assert tuple(saved) == (stamp, posting)
    assert store.latest_twd_transaction_dates() == {result["account_no"]: date(2026, 8, 30)}


@pytest.mark.parametrize("collect", [False, True])
@pytest.mark.parametrize("bad_date", ["2026/02/30", "not-a-date"])
def test_legacy_malformed_dates_still_reject_without_writes(store, collect, bad_date):
    result = _bound_result()
    result["snapshot"]["gridRows"][0][0] = bad_date
    with pytest.raises((RuntimeError, ValueError)):
        if collect:
            EsunCrawler._validated_twd_history_result(
                result, identity=result["account_no"], start=date(2025, 8, 31), end=date(2026, 8, 30),
            )
        else:
            persist_collected("esun", {"twd_txn_results": [result], "history_coverage": _bound_coverage()}, store)
    assert store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0] == 0


@pytest.mark.parametrize("collect", [False, True])
@pytest.mark.parametrize("statuses", [("complete",), ("complete", "complete"), ("explicit_empty", "complete")])
def test_spa_outside_rows_survive_complete_account_coverage(store, collect, statuses):
    history = coverage(status="complete")
    windows = history["domains"][0]["windows"]
    windows[0].update(end="2026-08-15" if len(statuses) == 2 else "2026-08-31", status=statuses[0])
    if len(statuses) == 2:
        windows.append(dict(windows[0], start="2026-08-16", end="2026-08-31", status=statuses[1]))
    rows = [row(datetime="2024-01-01 12:00:00"), row(datetime="2099-01-01 12:00:00")]
    if collect:
        rows = [normalize_row(dict(txDate=stamp, txTime="12:00:00", debitCredit="DB", detailTitle="測試轉帳",
                                   amount=80, balance=4, demandDeptAcc=ACCOUNT),
                              account_no=ACCOUNT, currency="TWD", start=date(2026, 8, 1), end=date(2026, 8, 31))
                for stamp in ("2024/01/01", "2099/01/01")]
    before = deepcopy(history)
    assert persist_collected("esun", {"history_coverage": history, "twd_txns": rows}, store)["twd_txn_new"] == 2
    saved = store.conn.execute("SELECT account_no, txn_datetime, account_date FROM twd_transactions ORDER BY txn_datetime").fetchall()
    assert [tuple(r) for r in saved] == [(ACCOUNT, r["datetime"], None) for r in rows]
    assert history == before  # No invented date/window attribution.
    assert store.latest_twd_transaction_dates() == {ACCOUNT: date(2026, 8, 31)}


@pytest.mark.parametrize("case", ["unrelated", "empty_only", "missing_complete_account", "insufficient_rows", "malformed_date", "missing_rows"])
def test_spa_account_status_and_date_schema_guards_still_reject(store, case):
    history = coverage(status="complete")
    rows = [row()]
    if case == "unrelated":
        rows[0]["account_no"] = OTHER
    elif case == "empty_only":
        history["domains"][0]["windows"][0]["status"] = "explicit_empty"
    elif case == "missing_complete_account":
        domain = history["domains"][0]
        domain["expected"].append(dict(domain["expected"][0], identity=OTHER))
        domain["windows"].append(dict(domain["windows"][0], identity=OTHER))
    elif case == "insufficient_rows":
        windows = history["domains"][0]["windows"]
        windows[0]["end"] = "2026-08-15"
        windows.append(dict(windows[0], start="2026-08-16", end="2026-08-31"))
    elif case == "malformed_date":
        rows[0]["datetime"] = "2026-02-30 12:00:00"
    else:
        rows = []
    with pytest.raises(ValueError):
        persist_collected("esun", {"history_coverage": history, "twd_txns": rows}, store)
    assert store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0] == 0
    assert store.latest_twd_transaction_dates() == {}
