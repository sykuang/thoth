"""驗證 persist_dbs() 對 DBS digibank API 的 dashboard 入庫行為。

DBS 特徵：
- liabilities.creditCard.cards[] 包含 active + 失效歷史卡（含原花旗併購來的）
- 卡號特徵 '************7002' (12 個 * + 末四碼)
- isPrimaryCard / isDisplayImg 決定狀態
- assets.casa.accounts[] 多幣別帳戶（TWD/EUR/JPY/USD/AUD/CNY）
- paymentDetails 含當期帳單金額 + 截止日

使用者 DBS 沒消費 (TWD 0)，dashboard 不會觸發逐筆 transaction endpoint。
mechanism 正確 = cards + accounts + paymentDetails 完整入庫。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core import store as store_mod
from backend.core.persist import persist_dbs
from backend.core.store import BankStore


@pytest.fixture
def tmp_store(tmp_path: Path, monkeypatch) -> BankStore:
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))  # 雙保險：env 也設,即使 store.py 退化也不會 leak 進真 dbs.sqlite
    return BankStore("dbs")


def _make_collected(cards=None, accounts=None, payment=None, endpoints=None) -> dict:
    """構造一個最小可入庫的 dbs collected dict。"""
    return {
        "initial_url": "https://internet-banking.dbs.com.tw/digitw/overview",
        "final_url": "https://internet-banking.dbs.com.tw/digitw/overview",
        "title": "Internet Banking",
        "home_text": "總覽",
        "nav_items": [],
        "_all_endpoints": endpoints or ["liabilities", "assets"],
        "api_responses": {
            "liabilities": [{
                "url": "https://internet-banking.dbs.com.tw/digitw/liabilities",
                "resp": {
                    "creditCard": {
                        "cards": cards or [],
                        "paymentDetails": payment or {},
                    },
                },
            }],
            "assets": [{
                "url": "https://internet-banking.dbs.com.tw/digitw/assets",
                "resp": {
                    "casa": {
                        "accounts": accounts or [],
                    },
                },
            }],
        },
    }


# === 1. 完整 dashboard: 6 卡 + 7 帳戶 + paymentDetails 全入庫 ===
def test_full_dashboard_cards_accounts_payment(tmp_store: BankStore) -> None:
    """模擬使用者實測 DBS dashboard: 6 卡 (3 active + 3 失效) + 7 帳戶。"""
    cards = [
        {"cardNumber": "************7002", "cardDescription": "星展饗樂生活卡",
         "isPrimaryCard": True, "isDisplayImg": False, "cardId": "id1"},
        {"cardNumber": "************7003", "cardDescription": "星展饗樂生活卡",
         "isPrimaryCard": True, "isDisplayImg": True, "cardId": "id2"},
        {"cardNumber": "************7038", "cardDescription": "原花旗現金回饋卡",
         "isPrimaryCard": True, "isDisplayImg": False, "cardId": "id3"},
        {"cardNumber": "************7004", "cardDescription": "星展eco永續極簡卡",
         "isPrimaryCard": True, "isDisplayImg": True, "cardId": "id4"},
    ]
    accounts = [
        {"displayAccountNumber": "90000017050",
         "availableBalance": {"currency": "TWD", "domesticCurrencyBalance": 0},
         "schemeName": "臺幣數位存款", "schemeType": "ODA"},
        {"displayAccountNumber": "90000037041",
         "availableBalance": {"currency": "USD", "domesticCurrencyBalance": 0},
         "schemeName": "USD數位存款", "schemeType": "ODA"},
    ]
    payment = {
        "amount": 0.0, "dueDate": "2026-06-22", "minimumAmount": 0.0,
        "alreadyPaid": 0, "currency": "TWD",
    }

    delta = persist_dbs(_make_collected(cards=cards, accounts=accounts, payment=payment),
                       tmp_store)

    assert delta["bank"] == "dbs"
    # cards 全入庫（不論 active/失效）
    n_cards = tmp_store.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    assert n_cards == 4
    # accounts 全入庫
    n_acct = tmp_store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    assert n_acct == 2
    # paymentDetails 寫進 daily_metrics
    pay_metric = tmp_store.conn.execute(
        "SELECT payload_json FROM daily_metrics WHERE category='dbs_card_billing_summary'"
    ).fetchone()
    assert pay_metric is not None and '"due_date": "2026-06-22"' in pay_metric["payload_json"]


# === 2. 卡號 masking：12 個 * + 末四 → '****<last4>' ===
def test_card_no_masking_last4(tmp_store: BankStore) -> None:
    cards = [
        {"cardNumber": "************7003", "cardDescription": "饗樂",
         "isPrimaryCard": True, "isDisplayImg": True},
    ]
    persist_dbs(_make_collected(cards=cards), tmp_store)
    row = tmp_store.conn.execute("SELECT card_no FROM cards").fetchone()
    assert row["card_no"] == "****7003", "卡號應抽末四 + ****"


# === 3. 失效卡 + 有效卡 同卡描述 → 兩張都入庫（不去重） ===
def test_active_and_inactive_cards_both_kept(tmp_store: BankStore) -> None:
    """同 cardDescription 但 cardNumber 不同的兩張卡（過期 + 新卡）都要入庫。"""
    cards = [
        {"cardNumber": "************7002", "cardDescription": "星展饗樂生活卡",
         "isPrimaryCard": True, "isDisplayImg": False},
        {"cardNumber": "************7003", "cardDescription": "星展饗樂生活卡",
         "isPrimaryCard": True, "isDisplayImg": True},
    ]
    persist_dbs(_make_collected(cards=cards), tmp_store)
    rows = tmp_store.conn.execute("SELECT card_no FROM cards ORDER BY card_no").fetchall()
    assert [r["card_no"] for r in rows] == ["****7002", "****7003"], \
        "失效卡跟新卡都該入庫，不能去重"


# === 4. 多幣別帳戶 → balance_history TWD 累加 ===
def test_multi_currency_accounts_twd_aggregation(tmp_store: BankStore) -> None:
    accounts = [
        {"displayAccountNumber": "90000017050",
         "availableBalance": {"currency": "TWD", "domesticCurrencyBalance": 100000},
         "schemeName": "臺幣數位存款", "schemeType": "ODA"},
        {"displayAccountNumber": "90000037041",
         "availableBalance": {"currency": "USD", "domesticCurrencyBalance": 30000},
         "schemeName": "USD數位存款", "schemeType": "ODA"},
    ]
    persist_dbs(_make_collected(accounts=accounts), tmp_store)
    bh = tmp_store.conn.execute(
        "SELECT twd_balance FROM balance_history").fetchone()
    assert bh["twd_balance"] == 130000, \
        "domesticCurrencyBalance 應該全加 (TWD + USD 折算後也算 TWD)"


# === 5. 缺 liabilities 整段 → 不 crash ===
def test_missing_liabilities_safe(tmp_store: BankStore) -> None:
    data = _make_collected()
    # 移掉 liabilities
    data["api_responses"].pop("liabilities", None)
    delta = persist_dbs(data, tmp_store)
    assert tmp_store.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
    assert delta["card_billed_new"] == 0


# === 6. 缺 assets 整段 → 不 crash ===
def test_missing_assets_safe(tmp_store: BankStore) -> None:
    data = _make_collected()
    data["api_responses"].pop("assets", None)
    delta = persist_dbs(data, tmp_store)
    assert tmp_store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    assert delta["balance_days"] == 0


# === 7. cardNumber 空字串 → 不會崩潰，card_no 也空 ===
def test_empty_card_number_handled(tmp_store: BankStore) -> None:
    cards = [
        {"cardNumber": "", "cardDescription": "未知卡",
         "isPrimaryCard": True, "isDisplayImg": True},
    ]
    persist_dbs(_make_collected(cards=cards), tmp_store)
    # cardNumber 為空時 last4="" → number="" → upsert_cards 應該 skip
    n = tmp_store.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    assert n == 0, "空 cardNumber 應跳過"


# === 8. endpoints 都會寫進 dbs_endpoints metric ===
def test_endpoints_dump_to_metric(tmp_store: BankStore) -> None:
    eps = ["liabilities", "assets", "customer-profile"]
    persist_dbs(_make_collected(endpoints=eps), tmp_store)
    row = tmp_store.conn.execute(
        "SELECT payload_json FROM daily_metrics WHERE category='dbs_endpoints'"
    ).fetchone()
    assert row is not None
    assert "liabilities" in row["payload_json"]
    assert "assets" in row["payload_json"]
