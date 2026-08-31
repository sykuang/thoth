"""各銀行 pending fetch 失敗／部分成功時必須 fail-closed。"""
from __future__ import annotations

import pytest

from backend.banks.taishin import TaishinCrawler
from backend.core.persist.cathay import persist_cathay
from backend.core.persist.ctbc import persist_ctbc
from backend.core.persist.esun import persist_esun
from backend.core.persist.fubon import persist_fubon
from backend.core.persist.hsbc import persist_hsbc
from backend.core.persist.scsb import persist_scsb
from backend.core.persist.sinopac import _persist_sinopac as persist_sinopac
from backend.core.persist.taishin import persist_taishin
from backend.core.persist.ubot import persist_ubot
from backend.core.store import BankStore


STALE_ERROR_MESSAGES = (
    "系統錯誤，請稍後再試",
    "連線逾時",
    "登入失效",
    "Request timeout",
    "Please log in again",
    "An unexpected error occurred",
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("DB_BACKEND", raising=False)
    st = BankStore("failclosed", user_id=1)
    st.refresh_card_pending("unbilled", [{
        "card_no": "****1234", "date": "2026-07-01", "desc": "KEEP",
        "amount": 100, "currency": "TWD",
    }], rules=[])
    st.conn.execute(
        "UPDATE card_pending_txns SET category='購物', description_overwrite='KEEP'"
    )
    st.conn.commit()
    yield st
    st.close()


def _assert_kept(store):
    row = store.conn.execute(
        "SELECT category, description_overwrite FROM card_pending_txns"
    ).fetchone()
    assert row is not None
    assert row["category"] == "購物"
    assert row["description_overwrite"] == "KEEP"


@pytest.mark.parametrize("persist,data", [
    (persist_ctbc, {"card_api_dump": {"/twrbc-card/qu006/011": None}}),
    (persist_ctbc, {"card_api_dump": {
        "/twrbc-card/qu006/011": {
            "error": "session expired", "allItems": [],
        },
    }}),
    (persist_cathay, {"credit_card": {"unbilled_detail": None, "current_detail": None}}),
    (persist_cathay, {"credit_card": {
        "unbilled_detail": {
            "error": "session expired", "twdUnbilledConsumeDetail": [],
        },
        "current_detail": {
            "error": "session expired", "twdCurrentConsumeDetail": [],
        },
    }}),
    (persist_ubot, {"card_unbilled": None}),
    (persist_ubot, {"card_unbilled": {
        "error": "session expired", "CardList": [],
    }}),
    (persist_taishin, {"credit_card_parsed": None}),
    (persist_taishin, {"credit_card_parsed": {"pending_txns": []}}),
    (persist_sinopac, {"card_unbilled": {"latest_tx": {
        "ResultCode": "99", "Error": {"message": "auth failed"},
        "Result": {"Items": []},
    }}}),
])
def test_untrusted_structured_payload_never_clears_pending(store, persist, data):
    persist(data, store, rules=[])
    _assert_kept(store)


def test_hsbc_partial_card_unposted_fetch_never_clears_pending(store):
    cards = [
        {"id": "a", "maskedCardNumber": "4029-****-****-1111", "cardStatusDisplay": "ACTIVATED"},
        {"id": "b", "maskedCardNumber": "4029-****-****-2222", "cardStatusDisplay": "ACTIVATED"},
    ]
    receipts = [{
        "identity": card["maskedCardNumber"],
        "start": "2025-09-01",
        "end": "2026-08-31",
        "status": "explicit_empty",
        "pages": 1,
        "rows": 0,
    } for card in cards]
    persist_hsbc({
        "cards": cards,
        "card_detail": {
            cards[0]["maskedCardNumber"]: {
                "card_id": "a",
                "masked": cards[0]["maskedCardNumber"],
                "posted": [], "posted_receipt": receipts[0].copy(),
                "unposted": [], "unposted_ok": True,
            },
            cards[1]["maskedCardNumber"]: {
                "card_id": "b",
                "masked": cards[1]["maskedCardNumber"],
                "posted": [], "posted_receipt": receipts[1].copy(),
                "unposted": [], "unposted_ok": False,
            },
        },
        "history_coverage": {
            "version": 1,
            "mode": "full",
            "domains": [{
                "domain": "card_billed_transactions",
                "expected": [{
                    "identity": receipt["identity"],
                    "start": receipt["start"],
                    "end": receipt["end"],
                } for receipt in receipts],
                "windows": [receipt.copy() for receipt in receipts],
            }],
        },
    }, store, rules=[])
    _assert_kept(store)


@pytest.mark.parametrize("persist,data", [
    (persist_esun, {"card_transactions": []}),
    (persist_fubon, {
        "pending_page_text": "系統錯誤，請稍後再試",
        "pending_page_url": "https://ebank.example/error",
    }),
    (persist_fubon, {
        "pending_click_ok": False,
        "pending_page_text": "消費日期 消費說明 臺幣金額",
        "pending_page_url": "https://ebank.example/cccqu004/home",
    }),
])
def test_payload_without_success_marker_is_not_trusted(store, persist, data):
    persist(data, store, rules=[])
    _assert_kept(store)


@pytest.mark.parametrize("message", STALE_ERROR_MESSAGES)
def test_scsb_success_markers_plus_error_preserve_both_scopes(store, message):
    store.refresh_card_pending("current", [{
        "card_no": "****1234", "date": "2026-07-01", "desc": "CURRENT",
        "amount": 200, "currency": "TWD",
    }], rules=[])
    delta = persist_scsb({"card_inquiry": {"leaves": {
        "unbilled": {
            "nav": {"ok": True},
            "text": f"You currently have no new transactions\n{message}",
        },
        "current": {
            "nav": {"ok": True},
            "text": f"No real-time transaction records\n{message}",
        },
    }}}, store, rules=[])
    assert delta["card_unbilled"] == 1
    assert delta["card_current"] == 1
    counts = dict(store.conn.execute(
        "SELECT scope, COUNT(*) FROM card_pending_txns GROUP BY scope"
    ).fetchall())
    assert counts["unbilled"] == 1
    assert counts["current"] == 1


def test_scsb_second_scope_failure_rolls_back_first_scope(store, monkeypatch):
    original = store.refresh_card_pending

    def fail_current(scope, *args, **kwargs):
        if scope == "current":
            raise RuntimeError("current refresh failed")
        return original(scope, *args, **kwargs)

    monkeypatch.setattr(store, "refresh_card_pending", fail_current)
    with pytest.raises(RuntimeError, match="current refresh failed"):
        persist_scsb({
            "accounts": [{
                "account_no": "90000000111111", "currency": "TWD",
                "balance": "9", "type_header": "活儲存款",
            }],
            "card_inquiry": {"leaves": {
            "unbilled": {
                "nav": {"ok": True},
                "text": "You currently have no new transactions",
            },
            "current": {
                "nav": {"ok": True},
                "text": "No real-time transaction records",
            },
        }}}, store, rules=[])
    _assert_kept(store)
    row = store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()
    assert row is not None and row[0] == 0


def test_scsb_wrong_page_never_clears_current(store):
    store.refresh_card_pending("current", [{
        "card_no": "****1234", "date": "2026-07-01", "desc": "CURRENT",
        "amount": 200, "currency": "TWD",
    }], rules=[])
    delta = persist_scsb({"card_inquiry": {"leaves": {"current": {
        "nav": {"ok": False}, "text": "臺幣帳戶餘額及交易明細",
    }}}}, store, rules=[])
    assert delta["card_current"] == 1
    count = store.conn.execute(
        "SELECT COUNT(*) AS n FROM card_pending_txns WHERE scope='current'"
    ).fetchone()
    assert count["n"] == 1


def test_taishin_generic_heading_with_error_is_not_success(store):
    crawler = TaishinCrawler.__new__(TaishinCrawler)
    parsed = crawler._parse_credit_card_page(
        "查詢信用卡明細\n即時消費紀錄\n系統錯誤，請稍後再試")
    assert parsed["fetch_ok"] is False
    persist_taishin({"credit_card_parsed": parsed}, store, rules=[])
    _assert_kept(store)


@pytest.mark.parametrize("message", [
    "您的連線已逾時，請重新登入",
    "請重新登入",
    "登入失效",
    "Connection timed out",
    "Request timeout",
    "Please log in again",
    "An unexpected error occurred",
])
def test_taishin_stale_headers_with_login_error_are_not_success(store, message):
    store.refresh_card_pending("realtime", [{
        "card_no": "****1234", "date": "2026-07-01", "desc": "REALTIME",
        "amount": 300, "currency": "TWD",
    }], rules=[])
    crawler = TaishinCrawler.__new__(TaishinCrawler)
    parsed = crawler._parse_credit_card_page(
        "即時消費紀錄\n消費日期\n消費時間\n消費明細\n授權結果\n" + message)
    assert parsed["fetch_ok"] is False
    delta = persist_taishin({"credit_card_parsed": parsed}, store, rules=[])
    assert delta["card_current"] == 1
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM card_pending_txns WHERE scope='realtime'"
    ).fetchone()["n"] == 1


def test_taishin_realtime_table_headers_are_success():
    crawler = TaishinCrawler.__new__(TaishinCrawler)
    parsed = crawler._parse_credit_card_page(
        "即時消費紀錄\n消費日期\n消費時間\n消費明細\n授權結果\n查無資料")
    assert parsed["fetch_ok"] is True


def test_taishin_explicit_target_page_success_sweeps_realtime(store):
    store.refresh_card_pending("realtime", [{
        "card_no": "****1234", "date": "2026-07-01", "desc": "REALTIME",
        "amount": 300, "currency": "TWD",
    }], rules=[])
    persist_taishin({"credit_card_parsed": {
        "fetch_ok": True, "pending_txns": [],
    }}, store, rules=[])
    count = store.conn.execute(
        "SELECT COUNT(*) AS n FROM card_pending_txns WHERE scope='realtime'"
    ).fetchone()
    assert count["n"] == 0


@pytest.mark.parametrize("message", STALE_ERROR_MESSAGES)
def test_fubon_success_markers_plus_error_preserve_realtime(store, message):
    store.refresh_card_pending("realtime", [{
        "card_no": "****1234", "date": "2026-07-01", "desc": "REALTIME",
        "amount": 300, "currency": "TWD",
    }], rules=[])
    delta = persist_fubon({
        "pending_click_ok": True,
        "pending_page_text": f"消費日期 消費說明 臺幣金額\n{message}",
        "pending_page_url": "https://ebank.example/cccqu004/home",
    }, store, rules=[])
    assert delta["card_current"] == 1
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM card_pending_txns WHERE scope='realtime'"
    ).fetchone()["n"] == 1


def test_fubon_failed_click_with_stale_table_preserves_realtime_and_reports_count(store):
    store.refresh_card_pending("realtime", [{
        "card_no": "****1234", "date": "2026-07-01", "desc": "REALTIME",
        "amount": 300, "currency": "TWD",
    }], rules=[])
    delta = persist_fubon({
        "pending_click_ok": False,
        "pending_page_text": "消費日期 消費說明 臺幣金額",
        "pending_page_url": "https://ebank.example/cccqu004/home",
    }, store, rules=[])
    assert delta["card_current"] == 1
    count = store.conn.execute(
        "SELECT COUNT(*) AS n FROM card_pending_txns WHERE scope='realtime'"
    ).fetchone()
    assert count["n"] == 1


def test_fubon_explicit_empty_sweeps_realtime_only(store):
    store.refresh_card_pending("realtime", [{
        "card_no": "****1234", "date": "2026-07-01", "desc": "REALTIME",
        "amount": 300, "currency": "TWD",
    }], rules=[])
    delta = persist_fubon({
        "pending_click_ok": True,
        "pending_page_text": "系統訊息\n查無相關資料",
        "pending_page_url": "https://ebank.example/cccqu004/home",
    }, store, rules=[])
    assert delta["card_current"] == 0
    count = store.conn.execute(
        "SELECT COUNT(*) AS n FROM card_pending_txns WHERE scope='realtime'"
    ).fetchone()
    assert count["n"] == 0


@pytest.mark.parametrize("persist,data", [
    (persist_esun, {"card_transactions": [], "card_transactions_ok": True}),
    (persist_cathay, {"credit_card": {
        "unbilled_detail": {"twdUnbilledConsumeDetail": []},
    }}),
    (persist_ubot, {"card_unbilled": {"CardList": []}}),
    (persist_sinopac, {"card_unbilled": {"latest_tx": {
        "ResultCode": "00", "Error": None, "Result": {"Items": []},
    }}}),
    (persist_scsb, {"card_inquiry": {"leaves": {"unbilled": {
        "nav": {"ok": True}, "text": "You currently have no new transactions",
    }}}}),
])
def test_explicit_successful_empty_response_sweeps_pending(store, persist, data):
    persist(data, store, rules=[])
    count = store.conn.execute(
        "SELECT COUNT(*) AS n FROM card_pending_txns WHERE scope='unbilled'"
    ).fetchone()
    assert count["n"] == 0
