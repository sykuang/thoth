"""E.SUN history/persistence prerequisites, isolated from live bank data."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.core.persist import persist_collected
from backend.core.store import BankStore

ACCOUNT = "9999999999999"
OTHER = "9999999999998"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    s = BankStore("esun_backport_test", user_id=7, source_account_id=91)
    yield s
    s.close()


def coverage(mode="full", *, end="2026-08-31", identity=ACCOUNT, status="explicit_empty"):
    return {"mode": mode, "domains": [{
        "domain": "twd_transactions",
        "expected": [{"identity": identity, "start": "2026-08-01", "end": end}],
        "windows": [{"identity": identity, "start": "2026-08-01", "end": end,
                     "status": status, "pages": 1}],
    }]}


def row(**changes):
    return {"account_no": ACCOUNT, "datetime": "2026-08-20 01:02:03",
            "account_date": None, "desc": "測試轉帳", "expend": 80,
            "income": None, "balance": 4} | changes


def test_full_replaces_stale_and_future_cursors_incremental_preserves_them(store):
    old = coverage(end="2026-12-31")
    old["domains"][0]["expected"].append({"identity": OTHER, "start": "2026-08-01", "end": "2026-12-31"})
    old["domains"][0]["windows"].append({"identity": OTHER, "start": "2026-08-01",
                                          "end": "2026-12-31", "status": "explicit_empty", "pages": 1})
    store.record_history_coverage_cursors(old)
    payload = {"history_coverage": coverage("incremental"), "twd_txns": []}
    persist_collected("esun", payload, store)
    assert {key: value.isoformat() for key, value in store.latest_twd_transaction_dates().items()} == {
        ACCOUNT: "2026-12-31", OTHER: "2026-12-31",
    }
    payload["history_coverage"]["mode"] = "full"
    persist_collected("esun", payload, store)
    assert {key: value.isoformat() for key, value in store.latest_twd_transaction_dates().items()} == {
        ACCOUNT: "2026-08-31",
    }


def test_late_cursor_failure_rolls_back_all_esun_writes_and_legacy_purge(store, monkeypatch):
    store.upsert_card_billed([{"card_no": "TEST-XXXX-XXXX-0001", "date": "2026-08-01",
                               "desc": "old", "amount": 10, "currency": "TWD"}])
    store.commit()
    tables = ("accounts", "balance_history", "twd_transactions", "cards", "card_billed_txns",
              "card_pending_txns", "daily_metrics", "sync_log", "history_transaction_cursors")
    before = {table: store.conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    payload = {
        "history_coverage": coverage(status="complete"), "twd_txns": [row()],
        "accounts": [{"account_no": ACCOUNT, "currency": "TWD", "balance": 4}],
        "card_quota": {"used_credit_twd": 20, "raw_text_sample": "secret"},
        "card_transactions_ok": True,
        "card_transactions": [{"card_no": "TEST-XXXX-XXXX-0002", "card_last4": "0002",
                               "status": "已入帳", "consume_date": "2026/08/20",
                               "merchant": "Test Shop", "billed_amount": 20}],
    }
    record = store.record_history_coverage_cursors

    def fail_after_cursor(*args, **kwargs):
        record(*args, **kwargs)
        raise RuntimeError("cursor failure")

    monkeypatch.setattr(store, "record_history_coverage_cursors", fail_after_cursor)
    with pytest.raises(RuntimeError, match="cursor failure"):
        persist_collected("esun", payload, store)
    after = {table: store.conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    assert after == before


@pytest.fixture
def pending_store(store):
    store.refresh_card_pending("unbilled", [{
        "card_no": "****0001", "date": "2026-08-01", "desc": "old pending",
        "amount": 10, "currency": "TWD",
    }], fetch_ok=True)
    store.record_history_coverage_cursors(coverage(end="2026-08-15"))
    assert not store.conn.in_transaction
    return store


@pytest.mark.parametrize("already_open", [False, True])
def test_empty_pending_refresh_cursor_failure_restores_committed_rows(pending_store, monkeypatch, already_open):
    store = pending_store
    tables = ("card_pending_txns", "history_transaction_cursors", "sync_log", "daily_metrics")
    before = {table: store.conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    if already_open:
        store.put_daily_metric("caller", {"value": 1}, "2026-08-01", commit=False)
    assert store.conn.in_transaction is already_open
    refresh = store.refresh_card_pending
    record = store.record_history_coverage_cursors
    refresh_transaction_states = []

    def observe_refresh(*args, **kwargs):
        refresh_transaction_states.append(store.conn.in_transaction)
        result = refresh(*args, **kwargs)
        refresh_transaction_states.append(store.conn.in_transaction)
        return result

    def fail_after_cursor(*args, **kwargs):
        record(*args, **kwargs)
        assert store.conn.in_transaction
        assert store.latest_twd_transaction_dates()[ACCOUNT].isoformat() == "2026-08-31"
        raise RuntimeError("cursor failure")

    monkeypatch.setattr(store, "refresh_card_pending", observe_refresh)
    monkeypatch.setattr(store, "record_history_coverage_cursors", fail_after_cursor)
    with pytest.raises(RuntimeError, match="cursor failure"):
        persist_collected("esun", {
            "history_coverage": coverage(), "twd_txns": [],
            "card_transactions": [], "card_transactions_ok": True,
        }, store)
    assert not store.conn.in_transaction
    after = {table: store.conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    assert after == before
    assert refresh_transaction_states == [True, True]


@pytest.mark.parametrize("already_open", [False, True])
def test_empty_pending_refresh_commits_with_cursor(pending_store, already_open):
    store = pending_store
    if already_open:
        store.put_daily_metric("caller", {"value": 1}, "2026-08-01", commit=False)
    assert store.conn.in_transaction is already_open
    delta = persist_collected("esun", {
        "history_coverage": coverage(), "twd_txns": [],
        "card_transactions": [], "card_transactions_ok": True,
    }, store)
    assert delta["card_unbilled"] == 0
    assert not store.conn.in_transaction
    store.conn.rollback()  # Success must commit even an already-open transaction.
    assert store.conn.execute("SELECT * FROM card_pending_txns").fetchall() == []
    assert store.latest_twd_transaction_dates()[ACCOUNT].isoformat() == "2026-08-31"
    assert store.conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] == int(already_open)


def test_jsf_complete_without_total_count_and_explicit_empty_marker(store):
    result = {"account_no": ACCOUNT, "start": "2026-08-01", "end": "2026-08-31",
              "status": "complete", "snapshot": {
                  "busy": False, "evidenceFresh": True,
                  "pager": {"present": False, "actionableNext": 0},
                  "hasGrid": True, "gridCandidateCount": 1, "gridRowCount": 1,
                  "gridRows": [["2026/08/20", "01:02:03", "測試轉帳", "80", "", "4"]],
                  "emptyMarker": None,
              }}
    payload = {"history_coverage": coverage(status="complete"), "twd_txn_results": [result]}
    assert persist_collected("esun", payload, store)["twd_txn_new"] == 1
    assert store.latest_twd_transaction_dates()[ACCOUNT].isoformat() == "2026-08-31"
    empty = deepcopy(result)
    empty["status"] = "explicit_empty"
    empty["snapshot"].update({"hasGrid": False, "gridCandidateCount": 0,
                              "gridRowCount": 0, "totalCount": 0, "gridRows": [],
                              "emptyMarker": "查無符合資料！"})
    assert persist_collected("esun", {"history_coverage": coverage(status="explicit_empty"),
                                      "twd_txn_results": [empty]}, store)["twd_txn_new"] == 0


def test_spa_explicit_empty_advances_cursor_but_complete_requires_rows(store):
    assert persist_collected("esun", {"history_coverage": coverage(), "twd_txns": []}, store)["twd_txn_new"] == 0
    assert store.latest_twd_transaction_dates()[ACCOUNT].isoformat() == "2026-08-31"
    with pytest.raises(ValueError, match="complete window lacks rows"):
        persist_collected("esun", {"history_coverage": coverage(status="complete"),
                                   "twd_txns": [], "accounts": [{"account_no": OTHER}]}, store)
    assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_esun_debug_payloads_never_enter_metrics_or_sync_log(store):
    payload = {"history_coverage": coverage(), "twd_txns": [],
               "card_quota": {"credit_limit_twd": 100, "raw_text_sample": "private-sentinel"},
               "card_txn_frames": [{"text_preview": "private-sentinel"}],
               "card_pay_history": {"raw_text": "private-sentinel"},
               "frames": [{"url": "private-sentinel"}],
               "main_text": "private-sentinel", "_all_endpoints": ["private-sentinel"]}
    persist_collected("esun", payload, store)
    saved = store.conn.execute("SELECT category, payload_json FROM daily_metrics").fetchall()
    assert {r["category"] for r in saved} == {"esun_card_quota"}
    assert "private-sentinel" not in "".join(r["payload_json"] for r in saved)
    assert "private-sentinel" not in store.conn.execute("SELECT summary FROM sync_log").fetchone()[0]


def test_cli_esun_removes_old_raw_dump_and_never_writes_new_one(tmp_path, monkeypatch):
    from cli import cli
    from backend.server import rules_repo
    raw = tmp_path / "backend" / "data" / "esun_collected.json"
    raw.parent.mkdir(parents=True)
    raw.write_text("old-sensitive-data")
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "cli" / "cli.py"))
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path / "db"))
    monkeypatch.setattr(rules_repo, "list_rules", lambda **_kw: [{}])

    class Crawler:
        HISTORY_COVERAGE_REQUIRED = True
        HISTORY_COVERAGE_DOMAINS = frozenset({"twd_transactions"})
        failed = False

        def configure_transaction_cursor(self, *_args):
            pass

        def run(self, **_kwargs):
            if self.failed:
                return {"error": "failed"}
            return {"data": {"history_coverage": coverage(), "twd_txns": [],
                             "card_bill_facts_ok": False}}

    crawler = Crawler()
    monkeypatch.setattr(cli, "_get_crawler", lambda _bank: (crawler, "https://example.invalid"))
    args = SimpleNamespace(bank="esun", headless=True)
    assert cli.cmd_sync(args) == 0
    assert not raw.exists()
    raw.write_text("old-sensitive-data")
    crawler.failed = True
    assert cli.cmd_sync(args) == 1
    assert not raw.exists()
