"""Phase 6 Plan A — /portfolio/summary endpoint tests.

語意定義 (使用者規則 2026-06-14):
  total_assets       = 銀行台幣存款 sum
  total_liabilities  = **上期帳單未繳** sum (only — 不含本月已刷)
  current_month_spending = 本月 consume_date 在 pending + billed 表 sum
  net_worth          = assets - liabilities (不扣本月消費)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.core.bank_data import KNOWN_BANKS

# Note: client/TestClient/app fixtures all come from conftest.py to ensure
# JWT_SECRET / Fernet key / tmp_path isolation. Do NOT add local `client`
# fixture here — see comment near auth_headers below.


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _current_month_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _seed_bank_db(root: Path, bank: str, *, balance: int | None = None,
                  balance_date: str | None = None,
                  loan_balance: int | None = None,
                  loan_accounts: list[dict] | None = None,
                  card_summary_category: str | None = None,
                  card_summary_payload: object | None = None,
                  card_summary_date: str | None = None,
                  pending_rows: list[dict] | None = None,
                  billed_rows: list[dict] | None = None,
                  fx_accounts: list[dict] | None = None) -> Path:
    """建一顆 mini sqlite, 只有 portfolio router 需要的 5 張表.

    2026-06-14：加 loan_balance + accounts 表（使用者鐵律：所有爬蟲都要處理貸款）。
    2026-06-14 fx: 加 fx_accounts (含 twd_transactions 模擬 sinopac JPY 帳戶) 給
                  /portfolio/summary 的 fx_assets_twd 用.
                  fx_accounts: list of dict {account_no, currency, balance}
    """
    path = root / f"{bank}.sqlite"
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS balance_history (
            snapshot_date TEXT PRIMARY KEY,
            twd_balance   INTEGER,
            fx_balance    INTEGER,
            loan_balance  INTEGER,
            updated_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS accounts (
            account_no       TEXT PRIMARY KEY,
            currency         TEXT,
            branch           TEXT,
            nickname         TEXT,
            type             TEXT,
            product_type     TEXT,
            raw_balance      REAL,
            raw_balance_date TEXT,
            excluded         INTEGER NOT NULL DEFAULT 0,
            updated_at       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_metrics (
            snapshot_date TEXT NOT NULL,
            category      TEXT NOT NULL,
            payload_json  TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            PRIMARY KEY (snapshot_date, category)
        );
        CREATE TABLE IF NOT EXISTS card_pending_txns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            card_no TEXT,
            consume_date TEXT,
            description TEXT,
            amount INTEGER,
            currency TEXT,
            refreshed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS card_billed_txns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_no TEXT,
            bill_date TEXT,
            currency TEXT,
            consume_date TEXT,
            post_date TEXT,
            description TEXT,
            amount INTEGER,
            consume_country TEXT,
            consume_currency TEXT,
            consume_amount REAL,
            first_seen TEXT,
            dedup_key TEXT,
            category TEXT
        );
        CREATE TABLE IF NOT EXISTS twd_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_no   TEXT NOT NULL,
            txn_datetime TEXT NOT NULL,
            account_date TEXT,
            description  TEXT,
            expend       INTEGER,
            income       INTEGER,
            balance      INTEGER,
            first_seen   TEXT,
            dedup_key    TEXT
        );
    """)
    now = _utcnow_iso()
    if balance is not None or loan_balance is not None:
        con.execute(
            "INSERT INTO balance_history (snapshot_date, twd_balance, loan_balance, updated_at) VALUES (?, ?, ?, ?)",
            (balance_date or "2026-06-13", balance, loan_balance, now),
        )
    if loan_accounts:
        for a in loan_accounts:
            con.execute(
                """INSERT INTO accounts (account_no, currency, branch, nickname, type, product_type,
                                          raw_balance, raw_balance_date, excluded, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (a["account_no"], a.get("currency", "TWD"), a.get("branch"),
                 a.get("nickname"), a.get("type"),
                 a.get("product_type", "loan"),
                 a.get("balance"), a.get("raw_balance_date"),
                 1 if a.get("excluded") else 0, now),
            )
    if card_summary_category and card_summary_payload is not None:
        con.execute(
            "INSERT INTO daily_metrics (snapshot_date, category, payload_json, updated_at) VALUES (?, ?, ?, ?)",
            (card_summary_date or "2026-06-13", card_summary_category,
             json.dumps(card_summary_payload), now),
        )
    for r in (pending_rows or []):
        con.execute(
            """INSERT INTO card_pending_txns
               (scope, card_no, consume_date, amount, currency, refreshed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (r.get("scope", "unbilled"), r.get("card_no"), r.get("consume_date"),
             r["amount"], r.get("currency", "TWD"), now),
        )
    for r in (billed_rows or []):
        con.execute(
            """INSERT INTO card_billed_txns
               (card_no, bill_date, currency, consume_date, post_date, description,
                amount, first_seen, dedup_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r.get("card_no"), r.get("bill_date"), r.get("currency", "TWD"),
             r.get("consume_date"), r.get("post_date"), r.get("description"),
             r["amount"], now, f"test-{id(r)}"),
        )
    # fx_accounts: 模擬 sinopac JPY 帳戶 — accounts 表 + twd_transactions 都要插
    # 2026-06-14 (excluded): fx_accounts dict 支援 excluded: bool (預設 False)
    for a in (fx_accounts or []):
        con.execute(
            """INSERT INTO accounts (account_no, currency, branch, nickname, type, product_type, excluded, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (a["account_no"], a["currency"], None, a.get("nickname"),
             a.get("type"), a.get("product_type", "fx_deposit"),
             1 if a.get("excluded") else 0, now),
        )
        if a.get("balance") is not None:
            con.execute(
                """INSERT INTO twd_transactions
                   (account_no, txn_datetime, account_date, description, expend, income, balance, first_seen, dedup_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (a["account_no"], "2026-06-13T10:00:00", "2026-06-13",
                 "test", None, None, a["balance"], now, f"fx-{id(a)}"),
            )
    con.commit()
    con.close()
    return path


@pytest.fixture
def temp_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    return tmp_path


# `client` fixture 從 conftest.py 取得 (isolated: tmp_path + JWT_SECRET +
# Fernet key + reload), 之前本檔自定 local fixture 只 return TestClient(app)
# 把 conftest 的 isolation shadow 掉, 導致 CI runner 乾淨 env 跑 register/login
# JWT 永遠拿不到 → CI 從 init commit 起 30 次全紅。2026-06-18 修法：刪 local
# fixture, 改用 conftest 的, CI 全綠。


@pytest.fixture
def auth_headers(client):
    email = f"portfolio-test-{datetime.now().timestamp()}@example.com"
    resp = client.post("/auth/register", json={"email": email, "password": "Password123!"})
    assert resp.status_code == 201, resp.text
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# Happy path — 多家銀行混合 schema
# ============================================================

def test_summary_aggregates_assets_and_real_liabilities(temp_data_root, client, auth_headers):
    """3 家混合 — 負債只取上期帳單未繳, 本月消費另算."""
    _current_month_str()

    # cathay: 88 萬存款 + 上期帳單 0 (已繳清)
    _seed_bank_db(temp_data_root, "cathay",
                  balance=888987, balance_date="2026-06-12",
                  card_summary_category="card_summary",
                  card_summary_payload={
                      "latest_bill": {"twd": {"billAmount": 0, "payBillStatus": "Paid"}},
                      "total_consumption": {"unpaid": 0, "current_balance": 0},
                  })
    # hsbc: 沒 balance, 兩張卡 outstanding sum 130393 (真實負債)
    _seed_bank_db(temp_data_root, "hsbc",
                  card_summary_category="card_summary",
                  card_summary_payload=[
                      {"outstanding": 18198.0, "min_payment": 1034.0},
                      {"outstanding": 112195.0, "min_payment": 15657.0},
                  ])
    # ubot: balance + Card 38647 (真實上期應繳, 不是 Unpaid 41065)
    _seed_bank_db(temp_data_root, "ubot",
                  balance=100000, balance_date="2026-06-11",
                  card_summary_category="card_summary",
                  card_summary_payload={
                      "TotalData": {"Unpaid": "41065", "Card": "38647"},
                  })

    r = client.get("/portfolio/summary", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["total_assets"] == 988987  # 888987 + 100000
    # 負債 = 上期帳單 sum (cathay 0 + hsbc 130393 + ubot 38647)
    assert body["total_liabilities"] == 169040
    assert body["net_worth"] == 988987 - 169040

    ubot = next(b for b in body["by_bank"] if b["bank"] == "ubot")
    assert ubot["liabilities"] == 38647  # 用 Card 不用 Unpaid

    cathay = next(b for b in body["by_bank"] if b["bank"] == "cathay")
    assert cathay["liabilities"] == 0  # payBillStatus='Paid' → 0


def test_summary_current_month_spending_from_pending_and_billed(temp_data_root, client, auth_headers):
    """本月消費 = pending(全) + billed 本月 consume_date sum.

    pending 表的本質是「最近同步抓到還沒出帳的」, refresh-by-scope 每次全清重寫,
    所以 pending row 永遠是當下未出帳的, 視同本月. billed 才用 consume_date 過濾.
    分期 12/12 在 pending、11/12 在 billed 都算本月消費.
    """
    month = _current_month_str()

    _seed_bank_db(temp_data_root, "ubot",
                  balance=100000, balance_date=f"{month}-13",
                  pending_rows=[
                      # 全部 pending 都算本月消費, 不論 consume_date (ubot 真實 case 是空)
                      {"amount": -41036, "currency": "TWD", "consume_date": ""},
                      {"amount": -29, "currency": "TWD", "consume_date": ""},
                  ],
                  billed_rows=[
                      # 本月 consume_date billed — 算 (分期 12/12 案例)
                      {"amount": -5000, "currency": "TWD",
                       "consume_date": f"{month}-05"},
                      # 上個月 consume_date billed — 不算
                      {"amount": -38647, "currency": "TWD",
                       "consume_date": "2026-05-15"},
                  ])
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    ubot = next(b for b in body["by_bank"] if b["bank"] == "ubot")
    # 41036 + 29 (pending 全部) + 5000 (billed 本月) = 46065
    assert ubot["current_month_spending"] == 46065
    assert body["current_month_spending"] == 46065


def test_summary_pending_is_NOT_added_to_liabilities(temp_data_root, client, auth_headers):
    """使用者鐵令: pending (本月已刷) 不算負債, 不論金額多大都不該進 liabilities."""
    month = _current_month_str()
    _seed_bank_db(temp_data_root, "cathay",
                  balance=1000000, balance_date=f"{month}-13",
                  card_summary_category="card_summary",
                  card_summary_payload={
                      "latest_bill": {"twd": {"billAmount": 0, "payBillStatus": "Paid"}},
                  },
                  pending_rows=[
                      {"amount": -50000, "currency": "TWD",
                       "consume_date": f"{month}-10"},
                  ])
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    assert body["total_liabilities"] == 0  # 上期帳單 0, pending 不進負債
    assert body["current_month_spending"] == 50000  # pending 進本月消費
    assert body["net_worth"] == 1000000  # = assets, 因為負債 = 0


def test_summary_foreign_pending_skipped(temp_data_root, client, auth_headers):
    """外幣 pending row 沒法算 TWD, 跳過 (禁推算)."""
    month = _current_month_str()
    _seed_bank_db(temp_data_root, "ctbc",
                  pending_rows=[
                      {"amount": -1500, "currency": "TWD",
                       "consume_date": f"{month}-10"},
                      {"amount": -100, "currency": "USD",  # ← skip
                       "consume_date": f"{month}-11"},
                  ])
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    ctbc = next(b for b in body["by_bank"] if b["bank"] == "ctbc")
    assert ctbc["current_month_spending"] == 1500  # 只算 TWD row


def test_summary_marks_stale_when_snapshot_too_old(temp_data_root, client, auth_headers):
    """超過 90 天的 snapshot 標 stale=True, 但仍算進總額."""
    very_old = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%d")
    _seed_bank_db(temp_data_root, "cathay",
                  balance=500000, balance_date=very_old)

    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    cathay = next(b for b in body["by_bank"] if b["bank"] == "cathay")
    assert cathay["stale"] is True
    assert cathay["assets"] == 500000
    assert body["total_assets"] == 500000


def test_summary_empty_when_no_databases(temp_data_root, client, auth_headers):
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    assert body["total_assets"] == 0
    assert body["total_liabilities"] == 0
    assert body["current_month_spending"] == 0
    assert body["net_worth"] == 0
    assert body["by_bank"] == []
    assert set(body["skipped"]) == set(KNOWN_BANKS)


def test_summary_requires_auth(temp_data_root, client):
    r = client.get("/portfolio/summary")
    assert r.status_code == 401


def test_summary_skips_banks_with_no_data_at_all(temp_data_root, client, auth_headers):
    _seed_bank_db(temp_data_root, "cathay")  # 全 None, 沒 row
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    assert "cathay" in body["skipped"]


def test_summary_sinopac_card_summary_parser(temp_data_root, client, auth_headers):
    """sinopac schema: [{SubInfo: [[{DataText: '本期應繳', DataValue: '12345'}, ...]]}]"""
    payload = [{
        "SubInfo": [[
            {"DataText": "幣別", "DataValue": "000"},
            {"DataText": "本期應繳", "DataValue": "12345"},
            {"DataText": "信用額度", "DataValue": "409,000"},
        ]],
    }]
    _seed_bank_db(temp_data_root, "sinopac",
                  balance=500000,
                  card_summary_category="card_summary",
                  card_summary_payload=payload)
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    sinopac = next(b for b in body["by_bank"] if b["bank"] == "sinopac")
    assert sinopac["liabilities"] == 12345


def test_summary_cathay_paid_status_returns_zero(temp_data_root, client, auth_headers):
    """payBillStatus='Paid' → 不論 billAmount 多少, 負債 = 0."""
    _seed_bank_db(temp_data_root, "cathay",
                  balance=100000,
                  card_summary_category="card_summary",
                  card_summary_payload={
                      "latest_bill": {"twd": {"billAmount": 50000, "payBillStatus": "Paid"}},
                  })
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    cathay = next(b for b in body["by_bank"] if b["bank"] == "cathay")
    assert cathay["liabilities"] == 0  # Paid → 0


# ============================================================
# Loan / 貸款 (Phase 6 — 2026-06-14): SCSB 房貸/信貸 bug 修補測試
# ============================================================
#
# 使用者鐵律：「所有的爬蟲都應該處理好貸款的部分（信貸跟房貸）」
# 真實 bug：SCSB 西湖分行貸款 NT$20,589,800 被當資產灌進 total_assets。
# 修補後：貸款餘額走 balance_history.loan_balance 或 accounts.product_type='loan'，
# portfolio router 加進 total_liabilities 不算 total_assets。

def test_loan_balance_from_balance_history(temp_data_root, client, auth_headers):
    """貸款餘額存在 balance_history.loan_balance → 走 fast path."""
    _seed_bank_db(temp_data_root, "scsb",
                  balance=13_065,           # 真實活儲（小額）
                  loan_balance=-20_589_800,  # legacy signed row 讀取時須正規化為負債規模
                  )
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    assert body["total_assets"] == 13_065         # 不再灌進貸款
    assert body["total_loan"] == 20_589_800       # 拆出貸款獨立統計
    assert body["total_liabilities"] >= 20_589_800
    assert body["net_worth"] == 13_065 - 20_589_800
    scsb = next(b for b in body["by_bank"] if b["bank"] == "scsb")
    assert scsb["loan_balance"] == 20_589_800
    assert scsb["liabilities"] == 20_589_800


def test_loan_split_card_and_loan_totals(temp_data_root, client, auth_headers):
    """同銀行同時有信用卡未繳 + 貸款，total_liabilities = 兩者相加."""
    _seed_bank_db(temp_data_root, "ubot",
                  balance=100_000,
                  loan_balance=500_000,
                  card_summary_category="card_summary",
                  card_summary_payload={
                      "TotalData": {"Card": 38_647, "Unpaid": 41_065},
                  })
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    ubot = next(b for b in body["by_bank"] if b["bank"] == "ubot")
    assert ubot["card_unpaid"] == 38_647
    assert ubot["loan_balance"] == 500_000
    assert ubot["liabilities"] == 38_647 + 500_000
    assert body["total_card_unpaid"] == 38_647
    assert body["total_loan"] == 500_000
    assert body["total_liabilities"] == 38_647 + 500_000


def test_loan_only_no_other_data_still_shows(temp_data_root, client, auth_headers):
    """銀行只有貸款（無存款餘額無信用卡），依然被計入 by_bank."""
    _seed_bank_db(temp_data_root, "scsb",
                  loan_balance=1_000_000)
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    assert "scsb" not in body["skipped"]
    scsb = next(b for b in body["by_bank"] if b["bank"] == "scsb")
    assert scsb["assets"] is None
    assert scsb["loan_balance"] == 1_000_000
    assert body["total_assets"] == 0
    assert body["total_loan"] == 1_000_000


def test_loan_fallback_from_accounts_table(temp_data_root, client, auth_headers):
    """balance_history 沒 loan_balance, 走 accounts.product_type='loan' fallback."""
    _seed_bank_db(temp_data_root, "cathay",
                  balance=500_000,
                  loan_accounts=[{
                      "account_no": "90000000177043",
                      "product_type": "loan",
                      "type": "個人信貸",
                  }],
                  card_summary_category="balance_latest",
                  card_summary_payload={"twd": 500_000, "loan": 800_000},
                  )
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    cathay = next(b for b in body["by_bank"] if b["bank"] == "cathay")
    # fallback 從 balance_latest.loan 撈
    assert cathay["loan_balance"] == 800_000


def test_loan_fallback_sums_account_balances_without_snapshot(
    temp_data_root, client, auth_headers
):
    _seed_bank_db(
        temp_data_root,
        "dbs",
        balance=100_000,
        loan_accounts=[
            {"account_no": "DBS-L1", "product_type": "loan", "balance": 300_000},
            {"account_no": "DBS-L2", "product_type": "mortgage", "balance": -200_000},
        ],
    )

    body = client.get("/portfolio/summary", headers=auth_headers).json()

    assert body["total_loan"] == 500_000
    assert body["total_liabilities"] == 500_000
    assert body["net_worth"] == -400_000


def test_partial_account_loan_balances_use_complete_metric_fallback(
    temp_data_root, client, auth_headers
):
    _seed_bank_db(
        temp_data_root,
        "dbs",
        balance=100_000,
        loan_accounts=[
            {"account_no": "DBS-L1", "product_type": "loan", "balance": 300_000},
            {"account_no": "DBS-L2", "product_type": "loan", "balance": None},
        ],
        card_summary_category="balance_latest",
        card_summary_payload={"loan": 800_000},
    )

    body = client.get("/portfolio/summary", headers=auth_headers).json()

    assert body["total_loan"] == 800_000


def test_undated_account_loan_balance_stays_stale(
    temp_data_root, client, auth_headers
):
    _seed_bank_db(
        temp_data_root,
        "dbs",
        loan_accounts=[
            {"account_no": "DBS-L1", "product_type": "loan", "balance": 300_000},
        ],
    )

    body = client.get("/portfolio/summary", headers=auth_headers).json()
    dbs = next(row for row in body["by_bank"] if row["bank"] == "dbs")

    assert dbs["loan_balance"] == 300_000
    assert dbs["as_of"] is None
    assert dbs["stale"] is True


def test_partial_account_loan_balances_without_metric_return_unknown(
    temp_data_root, client, auth_headers
):
    _seed_bank_db(
        temp_data_root,
        "dbs",
        balance=100_000,
        loan_accounts=[
            {"account_no": "DBS-L1", "product_type": "loan", "balance": 300_000},
            {"account_no": "DBS-L2", "product_type": "loan", "balance": None},
        ],
    )

    body = client.get("/portfolio/summary", headers=auth_headers).json()
    dbs = next(row for row in body["by_bank"] if row["bank"] == "dbs")

    assert dbs["loan_balance"] is None
    assert body["total_loan"] == 0


def test_failed_fx_loan_conversion_uses_complete_metric_fallback(
    temp_data_root, client, auth_headers, monkeypatch
):
    from backend.server import fx_service

    monkeypatch.setattr(fx_service, "convert_to_twd", lambda _amount, _currency: None)
    _seed_bank_db(
        temp_data_root,
        "dbs",
        balance=100_000,
        loan_accounts=[
            {"account_no": "DBS-L1", "product_type": "loan", "balance": 300_000},
            {
                "account_no": "DBS-L2",
                "currency": "USD",
                "product_type": "loan",
                "balance": 10_000,
            },
        ],
        card_summary_category="balance_latest",
        card_summary_payload={"loan": 800_000},
    )

    body = client.get("/portfolio/summary", headers=auth_headers).json()

    assert body["total_loan"] == 800_000


def test_mixed_dated_and_undated_account_loans_stay_stale(
    temp_data_root, client, auth_headers
):
    _seed_bank_db(
        temp_data_root,
        "dbs",
        loan_accounts=[
            {
                "account_no": "DBS-L1",
                "product_type": "loan",
                "balance": 100_000,
                "raw_balance_date": "2026-08-06",
            },
            {"account_no": "DBS-L2", "product_type": "loan", "balance": 200_000},
        ],
    )

    body = client.get("/portfolio/summary", headers=auth_headers).json()
    dbs = next(row for row in body["by_bank"] if row["bank"] == "dbs")

    assert dbs["loan_balance"] == 300_000
    assert dbs["as_of"] is None
    assert dbs["stale"] is True


def test_account_loan_aggregate_uses_oldest_balance_date(
    temp_data_root, client, auth_headers
):
    _seed_bank_db(
        temp_data_root,
        "dbs",
        loan_accounts=[
            {
                "account_no": "DBS-L1",
                "product_type": "loan",
                "balance": 100_000,
                "raw_balance_date": "2026-08-01",
            },
            {
                "account_no": "DBS-L2",
                "product_type": "loan",
                "balance": 200_000,
                "raw_balance_date": "2026-08-06",
            },
        ],
    )

    body = client.get("/portfolio/summary", headers=auth_headers).json()
    dbs = next(row for row in body["by_bank"] if row["bank"] == "dbs")

    assert dbs["loan_balance"] == 300_000
    assert dbs["as_of"] == "2026-08-01"


def test_no_loan_data_zero_loan_field(temp_data_root, client, auth_headers):
    """完全沒貸款資料 → total_loan=0, by_bank.loan_balance=None."""
    _seed_bank_db(temp_data_root, "cathay", balance=500_000)
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    assert body["total_loan"] == 0
    cathay = next(b for b in body["by_bank"] if b["bank"] == "cathay")
    assert cathay["loan_balance"] is None


# ============================================================
# fx_assets_twd — 外幣帳戶 TWD 估值（使用者 2026-06-14 「總資產要加入外幣」）
# ============================================================

def test_foreign_currency_loan_is_not_counted_as_fx_asset(
    temp_data_root, client, auth_headers, monkeypatch
):
    from backend.server import fx_service

    monkeypatch.setattr(fx_service, "get_rate", lambda _currency: 30.0)
    monkeypatch.setattr(fx_service, "convert_to_twd", lambda amount, _currency: round(amount * 30))
    _seed_bank_db(
        temp_data_root,
        "cathay",
        balance=100_000,
        loan_accounts=[{
            "account_no": "LOAN-USD",
            "currency": "USD",
            "product_type": "loan",
            "balance": 10_000,
        }],
    )

    body = client.get("/portfolio/summary", headers=auth_headers).json()

    assert body["fx_assets_twd"] == 0
    assert body["total_loan"] == 300_000
    assert body["net_worth_with_fx"] == -200_000


def test_excluded_foreign_currency_loan_is_removed_in_twd(
    temp_data_root, client, auth_headers, monkeypatch
):
    from backend.server import fx_service

    monkeypatch.setattr(fx_service, "convert_to_twd", lambda amount, _currency: round(amount * 30))
    _seed_bank_db(
        temp_data_root,
        "cathay",
        balance=100_000,
        loan_accounts=[{
            "account_no": "LOAN-USD",
            "currency": "USD",
            "product_type": "loan",
            "balance": 10_000,
            "excluded": True,
        }],
    )

    body = client.get("/portfolio/summary", headers=auth_headers).json()

    assert body["fx_assets_twd"] == 0
    assert body["total_loan"] == 0
    assert body["net_worth_with_fx"] == 100_000


def test_fx_assets_twd_zero_when_no_fx_accounts(
    temp_data_root, client, auth_headers, monkeypatch
):
    """沒任何外幣帳戶 → fx_assets_twd=0, total_assets_with_fx == total_assets."""
    _seed_bank_db(temp_data_root, "cathay", balance=500_000)
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    assert body["fx_assets_twd"] == 0
    assert body["total_assets_with_fx"] == body["total_assets"] == 500_000
    assert body["net_worth_with_fx"] == body["net_worth"] == 500_000


def test_brokerage_account_totals_are_included_once_in_net_worth(
    temp_data_root, client, auth_headers, monkeypatch
):
    """SnapTrade account total 進淨資產；cash/positions 不得重複加總。"""
    from backend.server import db, fx_service

    _seed_bank_db(temp_data_root, "cathay", balance=100_000)
    def convert(amount, ccy):
        if amount == "bad":
            raise OverflowError("synthetic invalid brokerage amount")
        return round(float(amount) * {"USD": 31.62, "TWD": 1}[ccy.upper()])

    monkeypatch.setattr(fx_service, "convert_to_twd", convert)
    monkeypatch.setattr(db, "snaptrade_snapshot", lambda _user_id: {
        "accounts": [
            {"id": "bad", "balance_total": "bad", "balance_currency": "USD", "synced_at": "2026-08-08T09:00:00+00:00"},
            {"id": "missing-currency", "balance_total": "500", "balance_currency": None, "synced_at": "2026-08-08T09:00:00+00:00"},
            {"id": "ibkr", "balance_total": "1000", "balance_currency": "USD", "synced_at": "2026-08-08T10:00:00+00:00"},
            {"id": "schwab", "balance_total": "5000", "balance_currency": "TWD", "synced_at": "2026-08-08T11:00:00+00:00"},
            {"id": "missing", "balance_total": None, "balance_currency": "USD", "synced_at": "2026-08-08T11:00:00+00:00"},
        ],
        "balances": [{"account_id": "ibkr", "cash": "999999", "currency": "USD"}],
        "positions": [{"account_id": "ibkr", "market_value": "999999", "currency": "USD"}],
        "activities": [],
        "last_synced_at": "2026-08-08T11:00:00+00:00",
    })

    body = client.get("/portfolio/summary", headers=auth_headers).json()

    assert body["total_assets"] == 100_000
    assert body["fx_assets_twd"] == 0
    assert body["brokerage_assets_twd"] == 36_620
    assert body["total_assets_with_fx"] == 136_620
    assert body["net_worth_with_fx"] == 136_620
    assert body["as_of"] == "2026-08-08"


def test_fx_assets_twd_aggregates_fx_balance_with_rate(
    temp_data_root, client, auth_headers, monkeypatch
):
    """sinopac JPY 1,201,387 → 用 fx_service mock 回 0.2 → estimate 240,277."""
    from backend.server import fx_service
    # Mock fx_service.convert_to_twd 避免打網路 (test 隔離)
    monkeypatch.setattr(
        fx_service, "convert_to_twd",
        lambda amount, ccy: round(amount * 0.2) if ccy.upper() == "JPY" else None,
    )

    _seed_bank_db(temp_data_root, "sinopac",
                  balance=1_088_367,
                  fx_accounts=[{"account_no": "90000000187013",
                                "currency": "JPY", "balance": 1_201_387}])
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    assert body["total_assets"] == 1_088_367            # TWD only
    assert body["fx_assets_twd"] == 240_277             # JPY 估值
    assert body["total_assets_with_fx"] == 1_328_644    # 合計
    assert body["net_worth_with_fx"] == 1_328_644       # 無負債
    # by_bank 也帶 fx_assets_twd
    sinopac = next(b for b in body["by_bank"] if b["bank"] == "sinopac")
    assert sinopac["fx_assets_twd"] == 240_277


def test_fx_assets_twd_multiple_currencies_sum(
    temp_data_root, client, auth_headers, monkeypatch
):
    """sinopac JPY + dbs USD 各自換 TWD 後 sum."""
    from backend.server import fx_service
    rates = {"JPY": 0.2, "USD": 31.695, "CNY": 4.707}
    monkeypatch.setattr(
        fx_service, "convert_to_twd",
        lambda amount, ccy: round(amount * rates.get(ccy.upper(), 0)) if ccy.upper() in rates else None,
    )

    _seed_bank_db(temp_data_root, "sinopac",
                  balance=100_000,
                  fx_accounts=[{"account_no": "JPY1", "currency": "JPY", "balance": 1_000_000}])  # 200,000
    _seed_bank_db(temp_data_root, "dbs",
                  balance=0,  # dbs 無 TWD 餘額
                  fx_accounts=[
                      {"account_no": "USD1", "currency": "USD", "balance": 1_000},   # 31,695
                      {"account_no": "CNY1", "currency": "CNY", "balance": 5_000},   # 23,535
                  ])
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    assert body["fx_assets_twd"] == 200_000 + 31_695 + 23_535  # 255,230


def test_fx_assets_twd_skips_when_balance_null(
    temp_data_root, client, auth_headers, monkeypatch
):
    """fx 帳戶 row 有但 balance=None (還沒爬到) → 不算進 fx_assets_twd."""
    from backend.server import fx_service
    monkeypatch.setattr(fx_service, "convert_to_twd", lambda a, c: round(a * 0.2))

    _seed_bank_db(temp_data_root, "dbs",
                  balance=None,  # 沒 TWD
                  fx_accounts=[
                      {"account_no": "JPY_NULL", "currency": "JPY", "balance": None},
                  ])
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    # dbs balance=None + fx balance=None + 沒其他資料 → skipped
    assert "dbs" in body["skipped"]
    assert body["fx_assets_twd"] == 0


def test_fx_assets_twd_skips_when_fx_service_fails(
    temp_data_root, client, auth_headers, monkeypatch
):
    """fx_service 抓不到該幣別 → 該帳戶不算 (保守略過, 不 raise)."""
    from backend.server import fx_service
    monkeypatch.setattr(fx_service, "convert_to_twd", lambda a, c: None)  # 全失敗

    _seed_bank_db(temp_data_root, "sinopac",
                  balance=100_000,
                  fx_accounts=[{"account_no": "JPY1", "currency": "JPY", "balance": 999_999}])
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    # TWD 100k 還在, fx 算不出來 → fx_assets_twd=0
    assert body["total_assets"] == 100_000
    assert body["fx_assets_twd"] == 0
    assert body["total_assets_with_fx"] == 100_000


def test_fx_assets_twd_ignores_twd_currency_accounts(
    temp_data_root, client, auth_headers, monkeypatch
):
    """fx_accounts 裡若 currency='TWD' (異常情境) → 不應算進 fx_assets_twd."""
    from backend.server import fx_service
    monkeypatch.setattr(fx_service, "convert_to_twd", lambda a, c: round(a * 1.0))

    _seed_bank_db(temp_data_root, "sinopac",
                  balance=100_000,
                  fx_accounts=[
                      {"account_no": "TWD_FAKE", "currency": "TWD", "balance": 50_000},  # 異常
                      {"account_no": "JPY1", "currency": "JPY", "balance": 1_000},       # 正常 → 1000
                  ])
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    # 只算 JPY1, TWD_FAKE 不算進 fx_assets_twd 避免雙重計算 (balance_history 已含)
    assert body["fx_assets_twd"] == 1_000


# ============================================================
# Phase 6 (2026-06-14): excluded flag
# 使用者手動標「不納入淨資產統計」→ summary 跳過, /portfolio/accounts 帶 flag
# ============================================================

def test_excluded_fx_account_not_in_fx_assets_twd(
    temp_data_root, client, auth_headers, monkeypatch
):
    """excluded=True 的外幣帳戶 → 不算進 fx_assets_twd."""
    from backend.server import fx_service
    monkeypatch.setattr(
        fx_service, "convert_to_twd",
        lambda amount, ccy: round(amount * 0.2) if ccy.upper() == "JPY" else None,
    )
    _seed_bank_db(temp_data_root, "sinopac",
                  balance=100_000,
                  fx_accounts=[
                      {"account_no": "JPY_IN", "currency": "JPY", "balance": 1_000},      # 算 200
                      {"account_no": "JPY_OUT", "currency": "JPY", "balance": 999_999,    # 不算
                       "excluded": True},
                  ])
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    assert body["total_assets"] == 100_000
    assert body["fx_assets_twd"] == 200            # 只有 JPY_IN, JPY_OUT 跳過
    assert body["total_assets_with_fx"] == 100_200


def test_excluded_twd_account_deducted_from_total_assets(
    temp_data_root, client, auth_headers
):
    """excluded=True 的台幣帳戶 → 從 total_assets 扣 raw_balance."""
    # balance_history 100k = 全銀行台幣 aggregate;
    # 其中 80k 是 ACC_OUT (excluded), 20k 是 ACC_IN
    _seed_bank_db(temp_data_root, "sinopac", balance=100_000,
                  fx_accounts=[
                      {"account_no": "ACC_OUT", "currency": "TWD", "balance": 80_000,
                       "excluded": True},
                  ])
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    # 100k - 80k(excluded) = 20k
    assert body["total_assets"] == 20_000
    assert body["net_worth"] == 20_000


def test_excluded_account_in_accounts_list_keeps_flag(
    temp_data_root, client, auth_headers
):
    """/portfolio/accounts 應該回傳 excluded=true 給該帳戶."""
    _seed_bank_db(temp_data_root, "sinopac", balance=100_000,
                  fx_accounts=[
                      {"account_no": "JPY1", "currency": "JPY", "balance": 1000,
                       "excluded": True},
                      {"account_no": "JPY2", "currency": "JPY", "balance": 2000},
                  ])
    r = client.get("/portfolio/accounts", headers=auth_headers)
    rows = r.json()
    excluded_map = {row["account_no"]: row["excluded"] for row in rows}
    assert excluded_map["JPY1"] is True
    assert excluded_map["JPY2"] is False


def test_patch_account_excluded_sets_flag(temp_data_root, client, auth_headers):
    """PATCH /portfolio/accounts/{bank}/{account_no}/excluded 翻轉 flag."""
    _seed_bank_db(temp_data_root, "sinopac", balance=100_000,
                  fx_accounts=[
                      {"account_no": "ACC1", "currency": "TWD", "balance": 50_000},
                  ])
    # 先確認預設 excluded=false
    r = client.get("/portfolio/accounts", headers=auth_headers)
    assert next(a for a in r.json() if a["account_no"] == "ACC1")["excluded"] is False

    # PATCH → true
    r = client.patch(
        "/portfolio/accounts/sinopac/ACC1/excluded",
        json={"excluded": True},
        headers=auth_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["excluded"] is True
    assert body["account_no"] == "ACC1"
    assert body["bank"] == "sinopac"

    # 再 GET 確認落地
    r2 = client.get("/portfolio/accounts", headers=auth_headers)
    assert next(a for a in r2.json() if a["account_no"] == "ACC1")["excluded"] is True

    # 再 PATCH 翻回 false
    client.patch(
        "/portfolio/accounts/sinopac/ACC1/excluded",
        json={"excluded": False},
        headers=auth_headers,
    )
    r3 = client.get("/portfolio/accounts", headers=auth_headers)
    assert next(a for a in r3.json() if a["account_no"] == "ACC1")["excluded"] is False


def test_patch_account_excluded_404_unknown_bank(
    temp_data_root, client, auth_headers
):
    """未知 bank → 404."""
    r = client.patch(
        "/portfolio/accounts/unknown_bank/ACC1/excluded",
        json={"excluded": True},
        headers=auth_headers,
    )
    assert r.status_code == 404


def test_patch_account_excluded_404_unknown_account(
    temp_data_root, client, auth_headers
):
    """已知 bank 但 account_no 不存在 → 404."""
    _seed_bank_db(temp_data_root, "sinopac", balance=100_000)
    r = client.patch(
        "/portfolio/accounts/sinopac/NONEXISTENT/excluded",
        json={"excluded": True},
        headers=auth_headers,
    )
    assert r.status_code == 404


def test_excluded_does_not_affect_fx_assets_when_account_balance_none(
    temp_data_root, client, auth_headers, monkeypatch
):
    """excluded=True 但 balance=None 的外幣帳戶 → 本來就不算, deduct=0."""
    from backend.server import fx_service
    monkeypatch.setattr(
        fx_service, "convert_to_twd",
        lambda amount, ccy: round(amount * 0.2) if ccy.upper() == "JPY" else None,
    )
    _seed_bank_db(temp_data_root, "sinopac",
                  balance=100_000,
                  fx_accounts=[
                      {"account_no": "JPY_VALID", "currency": "JPY", "balance": 1_000},     # 200
                      {"account_no": "JPY_EMPTY", "currency": "JPY", "balance": None,        # None
                       "excluded": True},
                  ])
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    assert body["fx_assets_twd"] == 200
    assert body["total_assets"] == 100_000           # 沒 deduct


def test_excluded_twd_account_capped_at_zero(
    temp_data_root, client, auth_headers
):
    """excluded TWD raw_balance > balance_history.twd_balance → assets 夾 0, 不負數."""
    # balance_history=10k, excluded ACC raw_balance=999k → deduct 超量, 夾 0
    _seed_bank_db(temp_data_root, "sinopac", balance=10_000,
                  fx_accounts=[
                      {"account_no": "BIG_EX", "currency": "TWD", "balance": 999_000,
                       "excluded": True},
                  ])
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    assert body["total_assets"] == 0
    assert body["net_worth"] == 0


# ============================================================
# Phase 6 hotfix (2026-06-14): 貸款帳戶 excluded 要從 loan_balance 扣
# 使用者 bug 回報: 房貸 (product_type='mortgage') 設成 excluded 後負債沒扣
# Root cause: 原邏輯只判 currency==TWD → 把 loan 當存款扣 total_assets (錯,
#   loan 根本不算 total_assets); 改判 product_type in (loan/mortgage/credit_line)
#   → 從 bank_loan 扣
# ============================================================

def test_excluded_loan_account_deducted_from_liabilities(
    temp_data_root, client, auth_headers
):
    """使用者的房貸 case: 貸款 excluded → 從 loan_balance 扣, 不該扣 total_assets."""
    _seed_bank_db(
        temp_data_root, "scsb",
        balance=1_980_176,            # TWD 存款
        loan_balance=20_589_800,      # balance_history.loan_balance
        loan_accounts=[{              # accounts 表的房貸 row
            "account_no": "90000000257044",
            "product_type": "mortgage",
            "currency": "TWD",
            "balance": 20_589_800,
            "excluded": True,
        }],
    )
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    # 資產不變 (excluded 是 loan 不是存款 → 不該動 total_assets)
    assert body["total_assets"] == 1_980_176
    # 負債扣掉房貸
    assert body["total_loan"] == 0
    assert body["total_liabilities"] == 0   # 沒信用卡未繳
    # 淨資產 = 資產 - 負債 = 1,980,176 - 0
    assert body["net_worth"] == 1_980_176


def test_excluded_loan_does_not_affect_total_assets(
    temp_data_root, client, auth_headers
):
    """Regression: excluded loan 不能誤入 twd_excluded_deduct path 扣 total_assets."""
    _seed_bank_db(
        temp_data_root, "scsb",
        balance=500_000,
        loan_balance=1_000_000,
        loan_accounts=[{
            "account_no": "L1",
            "product_type": "loan",
            "currency": "TWD",
            "balance": 1_000_000,
            "excluded": True,
        }],
    )
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    # 資產不該被 loan 的 balance 扣 (loan 不在 total_assets bucket)
    assert body["total_assets"] == 500_000
    assert body["total_loan"] == 0


def test_excluded_credit_line_deducted_from_loan(
    temp_data_root, client, auth_headers
):
    """credit_line 跟 loan/mortgage 同 bucket — excluded → 從 loan_balance 扣."""
    _seed_bank_db(
        temp_data_root, "ubot",
        balance=100_000,
        loan_balance=300_000,
        loan_accounts=[{
            "account_no": "CL1",
            "product_type": "credit_line",
            "currency": "TWD",
            "balance": 300_000,
            "excluded": True,
        }],
    )
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    assert body["total_loan"] == 0
    assert body["total_assets"] == 100_000


def test_excluded_loan_capped_at_zero(temp_data_root, client, auth_headers):
    """loan_excluded_deduct 超過 loan_balance → 夾在 0."""
    _seed_bank_db(
        temp_data_root, "scsb",
        balance=500_000,
        loan_balance=100_000,             # balance_history 給 100k
        loan_accounts=[{
            "account_no": "BIG_LOAN",
            "product_type": "mortgage",
            "currency": "TWD",
            "balance": 999_000,           # accounts 給 999k (deduct 超量)
            "excluded": True,
        }],
    )
    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    assert body["total_loan"] == 0
    assert body["total_liabilities"] == 0


# ============================================================
# Phase 6 (2026-06-14 PM): card.excluded → current_month_spending 跳過
# ============================================================

def test_excluded_card_skips_current_month_spending(
    temp_data_root, client, auth_headers
):
    """cards.excluded=1 → 該卡 billed/pending 不算進 current_month_spending."""
    import sqlite3
    from datetime import datetime, timezone
    # 用 _seed_bank_db 建好基本 schema + balance_history, 再手動補 cards 表
    _seed_bank_db(temp_data_root, "ubot", balance=100_000)
    # 直接補 cards 表 + 兩筆 billed
    today_month = datetime.now(timezone.utc).strftime("%Y-%m")
    con = sqlite3.connect(str(temp_data_root / "ubot.sqlite"))
    con.execute("""CREATE TABLE IF NOT EXISTS cards (
        card_no TEXT PRIMARY KEY, name TEXT, association TEXT, type TEXT,
        is_cube INTEGER, excluded INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )""")
    con.execute("INSERT INTO cards (card_no, excluded, updated_at) VALUES ('C_IN', 0, '2026-06-14T00:00:00')")
    con.execute("INSERT INTO cards (card_no, excluded, updated_at) VALUES ('C_OUT', 1, '2026-06-14T00:00:00')")
    con.execute("""INSERT INTO card_billed_txns
        (card_no, bill_date, currency, consume_date, post_date, description,
         amount, first_seen, dedup_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("C_IN", today_month + "-15", "TWD", today_month + "-10", today_month + "-15",
         "正常刷卡", 1500, "2026-06-14T00:00:00", "k1"))
    con.execute("""INSERT INTO card_billed_txns
        (card_no, bill_date, currency, consume_date, post_date, description,
         amount, first_seen, dedup_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("C_OUT", today_month + "-15", "TWD", today_month + "-10", today_month + "-15",
         "排除卡刷", 9999, "2026-06-14T00:00:00", "k2"))
    con.commit()
    con.close()

    r = client.get("/portfolio/summary", headers=auth_headers)
    body = r.json()
    # 本月消費只算 C_IN
    assert body["current_month_spending"] == 1500


# ============================================================
# Phase 8.2 C (2026-06-14): account nickname_overwrite endpoint
# ============================================================
def test_patch_account_nickname_sets_and_clears(temp_data_root, client, auth_headers):
    """PATCH /portfolio/accounts/{bank}/{account_no}/nickname 設覆寫 + 清空."""
    _seed_bank_db(temp_data_root, "sinopac", balance=100_000,
                  fx_accounts=[
                      {"account_no": "ACC1", "currency": "TWD", "balance": 50_000,
                       "nickname": "銀行原暱稱"},
                  ])

    # 一開始 overwrite 應為 None
    r = client.get("/portfolio/accounts", headers=auth_headers)
    acc = next(a for a in r.json() if a["account_no"] == "ACC1")
    assert acc["nickname"] == "銀行原暱稱"
    assert acc.get("nickname_overwrite") is None

    # 設覆寫
    r = client.patch(
        "/portfolio/accounts/sinopac/ACC1/nickname",
        json={"nickname_overwrite": "我的薪轉戶"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["nickname_overwrite"] == "我的薪轉戶"
    assert body["bank"] == "sinopac"
    assert body["account_no"] == "ACC1"

    # 落地: raw nickname 不動 + overwrite 有
    r2 = client.get("/portfolio/accounts", headers=auth_headers)
    acc = next(a for a in r2.json() if a["account_no"] == "ACC1")
    assert acc["nickname"] == "銀行原暱稱"  # raw 不動
    assert acc["nickname_overwrite"] == "我的薪轉戶"

    # 清空 ""
    r = client.patch(
        "/portfolio/accounts/sinopac/ACC1/nickname",
        json={"nickname_overwrite": ""},
        headers=auth_headers,
    )
    assert r.json()["nickname_overwrite"] is None

    # 清空 None
    client.patch(
        "/portfolio/accounts/sinopac/ACC1/nickname",
        json={"nickname_overwrite": "重設"},
        headers=auth_headers,
    )
    r = client.patch(
        "/portfolio/accounts/sinopac/ACC1/nickname",
        json={"nickname_overwrite": None},
        headers=auth_headers,
    )
    assert r.json()["nickname_overwrite"] is None


def test_patch_account_nickname_404(temp_data_root, client, auth_headers):
    """unknown bank / unknown account → 404."""
    r = client.patch(
        "/portfolio/accounts/unknown_bank/X/nickname",
        json={"nickname_overwrite": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 404

    _seed_bank_db(temp_data_root, "sinopac", balance=100_000,
                  fx_accounts=[{"account_no": "REAL", "currency": "TWD", "balance": 1}])
    r = client.patch(
        "/portfolio/accounts/sinopac/NONEXISTENT/nickname",
        json={"nickname_overwrite": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 404
