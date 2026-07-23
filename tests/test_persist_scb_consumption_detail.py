"""驗證 persist_scb 處理 SCB consumptionDetail API 的 mechanism。

SCB 真實 API endpoint: /mobilebank/rest/creditcard/consumptionDetail

case 1: NF_000021「查無歷史帳單」(使用者實際情況) — 不應寫 card_billed_txns
case 2: 有 transactions (mock) — 應寫 billed_txns 含雙幣值
case 3: 有 transactions + 外幣 — 應寫 consume_currency/consume_amount
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.persist import persist_scb
from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    """獨立 BankStore for test，sqlite 寫在 tmp_path。"""
    from backend.core import store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path, raising=True)
    s = BankStore("scb_test")
    yield s
    s.close()


def test_scb_consumption_detail_no_data(store):
    """case 1: API 回 NF_000021 → 不寫 billed，但 meta 應 dump（使用者實際情況）。"""
    data = {
        "api_responses": {
            "consumptionDetail": [{
                "url": "https://.../consumptionDetail",
                "method": "POST",
                "status": 200,
                "resp": {
                    "header": {"code": "NF_000021", "message": "查無信用卡歷史帳單資料"},
                    "body": {},
                },
                "req_body": {
                    "body": {
                        "cardNo": "encrypted_blob_abc",
                        "startDate": "2025/06/13",
                        "endDate": "2026/06/13",
                    }
                },
            }],
        },
        "_all_endpoints": ["consumptionDetail"],
    }
    delta = persist_scb(data, store)

    # 不寫 billed
    assert delta["card_billed_new"] == 0
    rows = list(store.conn.execute("SELECT * FROM card_billed_txns"))
    assert len(rows) == 0

    # 但 meta 要 dump 證明 mechanism 跑了
    meta_rows = list(store.conn.execute(
        "SELECT payload_json FROM daily_metrics WHERE category='scb_consumption_detail_meta'"
    ))
    assert len(meta_rows) == 1
    meta = json.loads(meta_rows[0][0])
    assert meta["requests"] == 1
    assert meta["results"][0]["code"] == "NF_000021"
    assert meta["results"][0]["start_date"] == "2025/06/13"


def test_scb_consumption_detail_with_twd_only(store):
    """case 2: 有 TWD-only transactions → 寫 billed_txns，consume_currency=None。"""
    data = {
        "api_responses": {
            "consumptionDetail": [{
                "url": "https://.../consumptionDetail",
                "method": "POST",
                "status": 200,
                "resp": {
                    "header": {"code": "0000"},
                    "body": {
                        "transactionList": [
                            {
                                "transactionDate": "2025/12/10",
                                "merchantName": "全聯福利中心",
                                "amount": 850,
                                "currency": "TWD",
                                "cardNo": "9065-XXXX-XXXX-7052",
                            },
                        ]
                    },
                },
                "req_body": {
                    "body": {"cardNo": "encrypted", "startDate": "2025/06", "endDate": "2026/06"}
                },
            }],
        },
        "_all_endpoints": ["consumptionDetail"],
    }
    delta = persist_scb(data, store)

    assert delta["card_billed_new"] == 1
    rows = list(store.conn.execute(
        "SELECT card_no, description, amount, currency, consume_currency, consume_amount FROM card_billed_txns"
    ))
    assert len(rows) == 1
    card_no, desc, amt, cur, cc, ca = rows[0]
    assert card_no == "****7052"
    assert desc == "全聯福利中心"
    assert amt == 850
    assert cur == "TWD"
    assert cc is None  # TWD-only 不寫 consume_currency
    assert ca is None


def test_scb_consumption_detail_with_foreign_currency(store):
    """case 3: 外幣 transaction → consume_currency + consume_amount 都應寫入。"""
    data = {
        "api_responses": {
            "consumptionDetail": [{
                "resp": {
                    "header": {"code": "0000"},
                    "body": {
                        "transactionList": [
                            {
                                "transactionDate": "2025/11/05",
                                "merchantName": "AMAZON.COM",
                                "localAmount": 1850,        # TWD 入帳
                                "originalAmount": 59.99,    # USD 原幣
                                "originalCurrency": "USD",
                                "cardNo": "9057-XXXX-XXXX-7062",
                            },
                        ]
                    },
                },
                "req_body": {"body": {"cardNo": "encrypted"}},
            }],
        },
        "_all_endpoints": ["consumptionDetail"],
    }
    delta = persist_scb(data, store)

    assert delta["card_billed_new"] == 1
    rows = list(store.conn.execute(
        "SELECT card_no, description, amount, currency, consume_currency, consume_amount FROM card_billed_txns"
    ))
    assert len(rows) == 1
    card_no, desc, amt, cur, cc, ca = rows[0]
    assert card_no == "****7062"
    assert desc == "AMAZON.COM"
    assert amt == 1850          # TWD 入帳金額
    assert cur == "TWD"
    assert cc == "USD"           # 外幣標記
    assert abs(ca - 59.99) < 0.01  # 外幣原額


def test_scb_consumption_detail_missing_api_safe(store):
    """case 4: 沒打 consumptionDetail API → 不應 crash，也不寫 billed。"""
    data = {"api_responses": {}, "_all_endpoints": []}  # 完全沒 consumptionDetail
    delta = persist_scb(data, store)
    assert delta["card_billed_new"] == 0
    assert delta["bank"] == "scb"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
