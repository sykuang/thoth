"""DBS deposit account transaction history persistence.

DBS 存款交易明細從 overview 帳戶 row drilldown 觸發：
- historical-summary/inquiry：近月摘要
- transactions-history/inquiry：明細 rows（使用者目前帳戶可為空）
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.core.persist import persist_dbs
from backend.core.persist.dbs import _parse_dbs_twd_transactions
from backend.core.store import BankStore


@pytest.fixture
def dbs_store(tmp_path: Path, monkeypatch) -> Iterator[BankStore]:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("dbs", user_id=1)
    try:
        yield store
    finally:
        store.close()


def _base_collected(transactions: list[dict] | None = None) -> dict:
    return {
        "_all_endpoints": ["assets", "inquiry"],
        "api_responses": {
            "assets": [{
                "url": "https://internet-banking.dbs.com.tw/prd/api/tw/v1/assets",
                "resp": {
                    "casa": {
                        "accounts": [{
                            "accountId": "******87266",
                            "globalAccountId": "tQSHscy1nn0_Y-E",
                            "displayAccountNumber": "90000027054",
                            "schemeName": "臺幣數位存款",
                            "schemeType": "ODA",
                            "availableBalance": {
                                "currency": "TWD",
                                "balance": "0",
                                "domesticCurrencyBalance": "0",
                            },
                        }],
                    },
                },
            }],
            "inquiry": [
                {
                    "url": "https://internet-banking.dbs.com.tw/prd/api/tw/v1/deposit-accounts-transactions-service/banking/deposit-accounts/historical-summary/inquiry",
                    "req_body": {
                        "globalAccountId": "tQSHscy1nn0_Y-E",
                        "txnEndDateTime": {"value": "2026-07-01", "format": "yyyy-MM-dd"},
                    },
                    "resp": {"historicalSummaries": [{"month": "2026-06"}]},
                },
                {
                    "url": "https://internet-banking.dbs.com.tw/prd/api/tw/v1/deposit-accounts-transactions-service/banking/deposit-accounts/transactions-history/inquiry",
                    "req_body": {
                        "globalAccountId": "tQSHscy1nn0_Y-E",
                        "currencyWallet": "TWD",
                    },
                    "resp": {
                        "pageInfo": {"fetchedRecords": len(transactions or []), "totalRecords": len(transactions or [])},
                        "transactions": transactions or [],
                    },
                },
            ],
        },
    }


def test_dbs_empty_transactions_history_writes_metrics_but_no_rows(dbs_store: BankStore) -> None:
    """使用者目前 DBS 帳戶無交易：persist 應誠實 0 筆，但保留 endpoint metric。"""
    delta = persist_dbs(_base_collected(transactions=[]), dbs_store, rules=None)

    assert delta["twd_txn_new"] == 0
    assert dbs_store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0] == 0
    categories = [r[0] for r in dbs_store.conn.execute("SELECT category FROM daily_metrics ORDER BY category")]
    assert "dbs_twd_historical_summary" in categories
    assert "dbs_twd_transactions_history" in categories


def test_parse_dbs_twd_transactions_flexible_shape() -> None:
    """先鎖定 transactions-history envelope；有資料時 flexible parser 能產生 twd rows。"""
    transactions = [
        {
            "accountingDate": {"value": "2026-07-02", "format": "yyyy-MM-dd"},
            "transactionDate": {"value": "2026-07-01T12:34:56+0800", "format": "yyyy-MM-dd'T'HH:mm:ssZ"},
            "transactionCategory": "轉帳",
            "remarks": "測試轉出",
            "debitAmount": {"balance": "1200", "currency": "TWD"},
            "balance": {"balance": "8800", "currency": "TWD"},
        },
        {
            "postingDate": "2026/07/03",
            "transactionType": "入息",
            "transactionDescription": "利息",
            "amount": {"balance": "5", "currency": "TWD"},
            "direction": "credit",
            "runningBalance": {"balance": "8805"},
        },
    ]
    data = _base_collected(transactions=transactions)
    rows = _parse_dbs_twd_transactions(
        data["api_responses"],
        {"tQSHscy1nn0_Y-E": "90000027054"},
    )

    assert rows == [
        {
            "account_no": "90000027054",
            "datetime": "2026-07-02",
            "account_date": "2026-07-01",
            "desc": "轉帳",
            "expend": 1200.0,
            "income": None,
            "balance": 8800.0,
            "counterparty_bank": None,
            "counterparty_acct": None,
            "memo": "測試轉出",
        },
        {
            "account_no": "90000027054",
            "datetime": "2026-07-03",
            "account_date": "2026-07-03",
            "desc": "入息",
            "expend": None,
            "income": 5.0,
            "balance": 8805.0,
            "counterparty_bank": None,
            "counterparty_acct": None,
            "memo": "利息",
        },
    ]


def test_persist_dbs_writes_twd_transactions_when_present(dbs_store: BankStore) -> None:
    transactions = [{
        "postingDate": "2026-07-03",
        "transactionDate": "2026-07-03",
        "transactionType": "入金",
        "amount": "100",
        "direction": "credit",
        "accountBalance": "100",
    }]

    delta = persist_dbs(_base_collected(transactions=transactions), dbs_store, rules=None)

    assert delta["twd_txn_new"] == 1
    row = dbs_store.conn.execute(
        "SELECT account_no, txn_datetime, account_date, description, expend, income, balance "
        "FROM twd_transactions"
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("90000027054", "2026-07-03", "2026-07-03", "入金", None, 100.0, 100.0)
