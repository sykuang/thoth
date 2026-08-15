"""Phase 5 (early) — /transactions routes end-to-end test.

Phase 5 (early) — /transactions routes 端到端測試。

涵蓋:
- 401 未登入
- 空 DB 回 200 + 空 list (使用者沒同步過)
- 寫真實資料進 cathay.sqlite + ctbc.sqlite, 確認跨庫聚合
- 分頁 (limit/offset)
- filter: bank / kind / since / until / q / category
- account_id 路徑
- /stats endpoint
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _register(client, email: str = "tx-user@palace.example", password: str = "SyntheticTestPassword02!"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_bank_db(data_root: Path, bank: str, twd: list[dict] | None = None,
                  billed: list[dict] | None = None, pending: list[dict] | None = None,
                  excluded_accounts: list[str] | None = None,
                  excluded_cards: list[str] | None = None) -> None:
    """直接插 schema + 假交易, 模擬 BankStore 寫過後的狀態.

    excluded_accounts: 額外建 accounts 表 + INSERT 標 excluded=1 的 account_no,
    給 Phase 6 excluded flag 測試用.
    excluded_cards: 額外建 cards 表 + INSERT 標 excluded=1 的 card_no.
    """
    path = data_root / f"{bank}.sqlite"
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE IF NOT EXISTS twd_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_no TEXT NOT NULL, txn_datetime TEXT NOT NULL, account_date TEXT,
        description TEXT, raw_description TEXT, expend INTEGER, income INTEGER, balance INTEGER,
        counterparty_bank TEXT, counterparty_acct TEXT, memo TEXT,
        first_seen TEXT NOT NULL, dedup_key TEXT NOT NULL, category TEXT,
        flow_type TEXT NOT NULL DEFAULT 'expense',
        is_subscription INTEGER NOT NULL DEFAULT 0,
        subcategory TEXT, legacy_category TEXT,
        income_category TEXT,
        description_overwrite TEXT,
        auto_excluded INTEGER NOT NULL DEFAULT 0
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS card_billed_txns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_no TEXT, bill_date TEXT, currency TEXT, consume_date TEXT,
        post_date TEXT, description TEXT, amount INTEGER,
        consume_country TEXT, consume_currency TEXT, consume_amount REAL,
        first_seen TEXT NOT NULL, dedup_key TEXT NOT NULL, category TEXT, txn_type TEXT,
        flow_type TEXT NOT NULL DEFAULT 'expense',
        is_subscription INTEGER NOT NULL DEFAULT 0,
        subcategory TEXT, legacy_category TEXT,
        income_category TEXT,
        description_overwrite TEXT,
        auto_excluded INTEGER NOT NULL DEFAULT 0
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS card_pending_txns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL, card_no TEXT, consume_date TEXT, post_date TEXT, description TEXT,
        amount INTEGER, currency TEXT, refreshed_at TEXT NOT NULL,
        category TEXT, txn_type TEXT,
        flow_type TEXT NOT NULL DEFAULT 'expense',
        is_subscription INTEGER NOT NULL DEFAULT 0,
        subcategory TEXT, legacy_category TEXT,
        income_category TEXT,
        description_overwrite TEXT,
        auto_excluded INTEGER NOT NULL DEFAULT 0
    )""")
    # Phase 6 (excluded): accounts 表只在有 excluded 帳戶時建
    if excluded_accounts:
        con.execute("""CREATE TABLE IF NOT EXISTS accounts (
            account_no TEXT PRIMARY KEY, currency TEXT, branch TEXT,
            nickname TEXT, type TEXT, product_type TEXT,
            raw_balance REAL, raw_balance_date TEXT,
            excluded INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )""")
        for acc_no in excluded_accounts:
            con.execute(
                "INSERT OR REPLACE INTO accounts (account_no, currency, excluded, updated_at) VALUES (?, ?, 1, ?)",
                (acc_no, "TWD", "2026-06-14T00:00:00"),
            )
    # Phase 6 (excluded): cards 表只在有 excluded 卡時建
    if excluded_cards:
        con.execute("""CREATE TABLE IF NOT EXISTS cards (
            card_no TEXT PRIMARY KEY, name TEXT, association TEXT, type TEXT,
            is_cube INTEGER, excluded INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )""")
        for card_no in excluded_cards:
            con.execute(
                "INSERT OR REPLACE INTO cards (card_no, name, excluded, updated_at) VALUES (?, ?, 1, ?)",
                (card_no, f"test card {card_no}", "2026-06-14T00:00:00"),
            )
    for i, t in enumerate(twd or []):
        con.execute(
            "INSERT INTO twd_transactions (account_no, txn_datetime, description, raw_description, expend, income, balance, counterparty_acct, memo, first_seen, dedup_key, category, flow_type, is_subscription, income_category, subcategory, auto_excluded) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["account_no"], t["datetime"], t["desc"], t.get("raw_desc", t["desc"]),
             t.get("expend"), t.get("income"),
             t.get("balance"), t.get("counterparty_acct"), t.get("memo"),
             "2026-06-13", f"twd-{bank}-{i}", t.get("category"),
             t.get("flow_type", "expense"), 1 if t.get("is_subscription") else 0,
             t.get("income_category"), t.get("subcategory"),
             1 if t.get("auto_excluded") else 0),
        )
    for i, t in enumerate(billed or []):
        con.execute(
            "INSERT INTO card_billed_txns (card_no, consume_date, post_date, description, amount, currency, first_seen, dedup_key, category, txn_type, flow_type, is_subscription, income_category, subcategory, auto_excluded) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["card_no"], t["date"], t.get("post_date", t["date"]), t["desc"], t["amount"],
             t.get("currency", "TWD"), "2026-06-13", f"billed-{bank}-{i}", t.get("category"),
             t.get("txn_type"), t.get("flow_type", "expense"), 1 if t.get("is_subscription") else 0,
             t.get("income_category"), t.get("subcategory"),
             1 if t.get("auto_excluded") else 0),
        )
    for i, t in enumerate(pending or []):
        con.execute(
            "INSERT INTO card_pending_txns (scope, card_no, consume_date, post_date, description, amount, currency, refreshed_at, category, txn_type, flow_type, is_subscription, income_category, subcategory, auto_excluded) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.get("scope", "unbilled"), t["card_no"], t["date"], t.get("post_date"), t["desc"],
             t["amount"], t.get("currency", "TWD"), "2026-06-13", t.get("category"),
             t.get("txn_type"), t.get("flow_type", "expense"), 1 if t.get("is_subscription") else 0,
             t.get("income_category"), t.get("subcategory"),
             1 if t.get("auto_excluded") else 0),
        )
    con.commit()
    con.close()


@pytest.fixture
def data_root(tmp_path):
    """conftest 已 setenv BANK_DATA_ROOT, 這裡只暴露 path."""
    import os
    return Path(os.environ["BANK_DATA_ROOT"])


# ============================================================
# 401 未登入
# ============================================================

def test_transactions_requires_auth(client):
    r = client.get("/transactions")
    assert r.status_code == 401


# ============================================================
# 空 DB → 200 + 空 list
# ============================================================

def test_transactions_empty_returns_empty(client):
    token = _register(client)
    r = client.get("/transactions", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 0
    assert body["items"] == []


# ============================================================
# 跨庫聚合 + 預設不限只回 100 筆 (page=0)
# ============================================================

def test_transactions_aggregates_across_banks(client, data_root):
    token = _register(client)
    # 註冊兩家銀行 account → resolve_banks 會列 [cathay, ctbc]
    for bank in ("cathay", "ctbc"):
        r = client.post("/accounts", json={"bank": bank, "label": "test"}, headers=_auth(token))
        assert r.status_code == 201, r.text

    _seed_bank_db(data_root, "cathay",
                  twd=[
                      {"account_no": "90007050", "datetime": "2026-06-01T10:00:00",
                       "desc": "ATM 提款", "expend": 1000, "income": 0, "balance": 50000,
                       "category": "提款"},
                      {"account_no": "90007050", "datetime": "2026-05-15T08:30:00",
                       "desc": "薪資匯入", "expend": 0, "income": 80000, "balance": 51000},
                  ],
                  billed=[
                      {"card_no": "****7016", "date": "2026-05-20", "desc": "全聯",
                       "amount": 350, "category": "餐飲"},
                  ])
    _seed_bank_db(data_root, "ctbc",
                  pending=[
                      {"card_no": "****7050", "date": "2026-06-10", "desc": "蝦皮",
                       "amount": 1200},
                      {"card_no": "****7050", "date": "2026-06-11", "desc": "全家",
                       "amount": 85, "category": "餐飲"},
                  ])

    r = client.get("/transactions", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 5  # 2 twd + 1 billed + 2 pending
    assert body["stats"]["by_bank"] == {"cathay": 3, "ctbc": 2}
    assert body["stats"]["by_kind"] == {"twd": 2, "billed": 1, "pending": 2}
    # 最新日期 (2026-06-11 ctbc pending) 應該排第一
    assert body["items"][0]["date"] == "2026-06-11"
    assert body["items"][0]["bank"] == "ctbc"
    # ATM 提款 amount -1000 (expense)
    twd_atm = next(t for t in body["items"] if t["description"] == "ATM 提款")
    assert twd_atm["amount"] == -1000
    # 薪資匯入 +80000
    salary = next(t for t in body["items"] if t["description"] == "薪資匯入")
    assert salary["amount"] == 80000
    # 信用卡消費自動 反號
    quan = next(t for t in body["items"] if t["description"] == "全聯")
    assert quan["amount"] == -350
    # account_no 末四 mask
    assert twd_atm["account_or_card"] == "****7050"


def test_transactions_returns_database_canonical_description_without_api_join(client, data_root):
    token = _register(client)
    client.post("/accounts", json={"bank": "cathay", "label": "test"}, headers=_auth(token))
    _seed_bank_db(data_root, "cathay", twd=[{
        "account_no": "90007050",
        "datetime": "2026-08-10T09:00:00",
        "raw_desc": "轉帳",
        "desc": "轉帳 - 0050FUND 基金配息",
        "memo": "0050FUND　基金配息",
        "counterparty_acct": "0050FUND",
        "expend": 0,
        "income": 4494,
        "balance": 10000,
    }])

    response = client.get("/transactions", headers=_auth(token))
    assert response.status_code == 200, response.text
    txn = response.json()["items"][0]
    assert txn["description"] == "轉帳 - 0050FUND 基金配息"
    assert txn["display_description"] == "轉帳 - 0050FUND 基金配息"
    assert txn["memo"] == "0050FUND　基金配息"
    assert txn["raw"]["raw_description"] == "轉帳"


# ============================================================
# 分頁
# ============================================================

def test_transactions_pagination(client, data_root):
    token = _register(client)
    client.post("/accounts", json={"bank": "cathay", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "cathay",
                  twd=[
                      {"account_no": "7016", "datetime": f"2026-06-{i:02d}T10:00:00",
                       "desc": f"交易 {i}", "expend": i * 100, "income": 0, "balance": 0}
                      for i in range(1, 26)  # 25 筆
                  ])

    r = client.get("/transactions?limit=10&offset=0", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 25
    assert len(body["items"]) == 10
    # 排序 desc, 第 1 筆是 06-25
    assert body["items"][0]["description"] == "交易 25"

    r = client.get("/transactions?limit=10&offset=20", headers=_auth(token))
    body = r.json()
    assert len(body["items"]) == 5  # 25-20=5
    assert body["items"][0]["description"] == "交易 5"


def test_transactions_limit_5000_allowed_for_client_side_filter(client, data_root):
    """Phase 9 C-2 (2026-06-19): limit 上限 1000 → 5000.

    Frontend client-side filter pivot 要求一次撈整個 period (一般 50-200 筆,
    極端 ~3000-5000). 此 test 確保 5000 不被 422 reject.
    """
    token = _register(client)
    client.post("/accounts", json={"bank": "cathay", "label": "x"}, headers=_auth(token))
    # 不需要 seed 5000 筆 row, 只驗 limit=5000 validator 通過
    r = client.get("/transactions?limit=5000", headers=_auth(token))
    assert r.status_code == 200, f"limit=5000 應該被接受, 實際: {r.status_code} {r.text}"

    # 邊界: 5001 應該被 reject
    r = client.get("/transactions?limit=5001", headers=_auth(token))
    assert r.status_code == 422


# ============================================================
# Filters
# ============================================================

def test_transactions_filter_by_kind(client, data_root):
    token = _register(client)
    client.post("/accounts", json={"bank": "cathay", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "cathay",
                  twd=[{"account_no": "1", "datetime": "2026-06-01T10:00:00",
                        "desc": "twd", "expend": 100, "income": 0, "balance": 0}],
                  billed=[{"card_no": "1", "date": "2026-06-01", "desc": "billed", "amount": 200}])

    r = client.get("/transactions?kind=twd", headers=_auth(token))
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["kind"] == "twd"


def test_transactions_filter_by_q(client, data_root):
    token = _register(client)
    client.post("/accounts", json={"bank": "cathay", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "cathay",
                  billed=[
                      {"card_no": "1", "date": "2026-06-01", "desc": "全聯福利中心", "amount": 100},
                      {"card_no": "1", "date": "2026-06-02", "desc": "蝦皮", "amount": 200},
                  ])

    r = client.get("/transactions?q=全聯", headers=_auth(token))
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["description"] == "全聯福利中心"


def test_transactions_filter_by_date_range(client, data_root):
    token = _register(client)
    client.post("/accounts", json={"bank": "cathay", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "cathay",
                  twd=[{"account_no": "1", "datetime": f"2026-06-{i:02d}T10:00:00",
                        "desc": f"t{i}", "expend": 1, "income": 0, "balance": 0}
                       for i in range(1, 11)])

    r = client.get("/transactions?since=2026-06-05&until=2026-06-08", headers=_auth(token))
    body = r.json()
    assert body["total"] == 4  # 05/06/07/08


def test_transactions_card_date_basis_post_filters_and_sorts_by_post_date(client, data_root):
    """card_date_basis=post lets users recognize card rows by posting date.

    Default remains consume date. In post mode, both billed and pending card rows
    use post_date for range filters, display date, and ordering; TWD rows remain
    on txn date.
    """
    token = _register(client, email="date-basis@p.com")
    client.post("/accounts", json={"bank": "cathay", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "cathay",
                  twd=[{"account_no": "A", "datetime": "2026-07-02T10:00:00",
                        "desc": "twd-post-irrelevant", "expend": 1, "income": 0, "balance": 0}],
                  billed=[
                      {"card_no": "C", "date": "2026-06-28", "post_date": "2026-07-03",
                       "desc": "cross-month-billed", "amount": 100},
                      {"card_no": "C", "date": "2026-07-02", "post_date": "2026-07-02",
                       "desc": "same-month-billed", "amount": 200},
                  ],
                  pending=[
                      {"card_no": "C", "date": "2026-06-29", "post_date": "2026-07-04",
                       "desc": "cross-month-pending", "amount": 300},
                  ])

    # Default consume-date mode excludes the 6/28 and 6/29 card rows from July.
    r = client.get(
        "/transactions?since=2026-07-01&until=2026-07-31",
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert [item["description"] for item in body["items"]] == [
        "twd-post-irrelevant",
        "same-month-billed",
    ]

    # Post-date mode includes them and exposes the recognized date as date.
    r = client.get(
        "/transactions?since=2026-07-01&until=2026-07-31&card_date_basis=post",
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert [item["description"] for item in body["items"]] == [
        "cross-month-pending",
        "cross-month-billed",
        "twd-post-irrelevant",
        "same-month-billed",
    ]
    by_desc = {item["description"]: item for item in body["items"]}
    assert by_desc["cross-month-billed"]["date"] == "2026-07-03"
    assert by_desc["cross-month-billed"]["consume_date"] == "2026-06-28"
    assert by_desc["cross-month-billed"]["post_date"] == "2026-07-03"
    assert by_desc["cross-month-pending"]["date"] == "2026-07-04"
    assert by_desc["cross-month-pending"]["consume_date"] == "2026-06-29"
    assert by_desc["cross-month-pending"]["post_date"] == "2026-07-04"


def test_card_drilldown_keeps_bank_level_installment_on_post_date(client, data_root):
    """Card scope keeps unattributed card rows without leaking deposits or sibling cards."""
    token = _register(client, email="bank-level-installment@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(
        data_root,
        "ubot",
        twd=[{
            "account_no": "A", "datetime": "2026-06-11T10:00:00",
            "desc": "deposit-row", "expend": 1, "income": 0, "balance": 0,
        }],
        billed=[
            {
                "card_no": "", "date": "2026-05-05", "post_date": "2026-06-11",
                "desc": "installment-01/12", "amount": 45756,
            },
            {
                "card_no": "CARD-1", "date": "2026-06-10", "post_date": "2026-06-11",
                "desc": "selected-card", "amount": 29,
            },
            {
                "card_no": "CARD-2", "date": "2026-06-10", "post_date": "2026-06-11",
                "desc": "sibling-card", "amount": 39,
            },
        ],
    )

    response = client.get(
        "/transactions?bank=ubot&card_no=CARD-1&since=2026-06-01&until=2026-06-30&card_date_basis=post",
        headers=_auth(token),
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert {item["description"] for item in items} == {"installment-01/12", "selected-card"}
    installment = next(item for item in items if item["description"] == "installment-01/12")
    assert installment["date"] == "2026-06-11"
    assert installment["card_no"] == ""


def test_transactions_stats_card_date_basis_post_buckets_by_post_date(client, data_root):
    """Stats date buckets also follow card_date_basis for card rows."""
    token = _register(client, email="date-basis-stats@p.com")
    client.post("/accounts", json={"bank": "cathay", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "cathay",
                  billed=[
                      {"card_no": "C", "date": "2026-06-28", "post_date": "2026-07-03",
                       "desc": "cross-month-billed", "amount": 100, "category": "購物"},
                  ],
                  pending=[
                      {"card_no": "C", "date": "2026-06-29", "post_date": "2026-07-04",
                       "desc": "cross-month-pending", "amount": 300, "category": "購物"},
                  ])

    r = client.get(
        "/transactions/stats?since=2026-07-01&until=2026-07-31",
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["total"] == 0

    r = client.get(
        "/transactions/stats?since=2026-07-01&until=2026-07-31&card_date_basis=post",
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert body["by_month"] == {"2026-07": 2}
    assert body["amount_by_month"]["2026-07"]["expense"] == 400
    assert body["amount_by_category"]["購物"] == 400

def test_transactions_by_account_id(client, data_root):
    token = _register(client)
    # 建兩個 account, 不同 bank
    r1 = client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    r2 = client.post("/accounts", json={"bank": "ctbc", "label": "主帳"}, headers=_auth(token))
    cathay_id = r1.json()["id"]
    _ = r2.json()["id"]
    _seed_bank_db(data_root, "cathay", twd=[
        {"account_no": "1", "datetime": "2026-06-01T10:00:00", "desc": "cathay-only",
         "expend": 100, "income": 0, "balance": 0}])
    _seed_bank_db(data_root, "ctbc", billed=[
        {"card_no": "1", "date": "2026-06-01", "desc": "ctbc-only", "amount": 200}])

    r = client.get(f"/transactions?account_id={cathay_id}", headers=_auth(token))
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["description"] == "cathay-only"


def test_transactions_by_account_id_not_owned_returns_404(client):
    token = _register(client)
    r = client.get("/transactions?account_id=99999", headers=_auth(token))
    assert r.status_code == 404


# ============================================================
# /stats
# ============================================================

def test_transactions_stats(client, data_root):
    token = _register(client)
    client.post("/accounts", json={"bank": "cathay", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "cathay",
                  twd=[{"account_no": "1", "datetime": "2026-06-15T10:00:00",
                        "desc": "薪資", "expend": 0, "income": 50000, "balance": 0}],
                  billed=[
                      {"card_no": "1", "date": "2026-05-01", "desc": "餐", "amount": 300,
                       "category": "餐飲"},
                      {"card_no": "1", "date": "2026-05-02", "desc": "餐2", "amount": 400,
                       "category": "餐飲"},
                  ])

    r = client.get("/transactions/stats", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert body["by_bank"]["cathay"] == 3
    assert body["by_kind"] == {"twd": 1, "billed": 2}
    assert body["by_month"]["2026-06"] == 1
    assert body["by_month"]["2026-05"] == 2
    assert body["by_category"]["餐飲"] == 2


# ============================================================
# L8.5 — amount sum stats: total income/expense/net, per-month, per-category
# ============================================================

def test_transactions_stats_includes_amount_sums(client, data_root):
    """新欄位: total_income / total_expense / total_net / amount_by_month / amount_by_category"""
    token = _register(client, email="amt-stats@p.com")
    client.post("/accounts", json={"bank": "cathay", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "cathay",
                  twd=[
                      {"account_no": "A", "datetime": "2026-06-15T10:00:00",
                       "desc": "salary", "expend": 0, "income": 50000, "balance": 0},
                      {"account_no": "A", "datetime": "2026-06-20T10:00:00",
                       "desc": "rent", "expend": 20000, "income": 0, "balance": 0},
                  ],
                  billed=[
                      {"card_no": "C", "date": "2026-06-01", "desc": "food", "amount": 1500,
                       "category": "餐飲"},
                      {"card_no": "C", "date": "2026-06-05", "desc": "food2", "amount": 800,
                       "category": "餐飲"},
                      {"card_no": "C", "date": "2026-05-01", "desc": "old food",
                       "amount": 500, "category": "餐飲"},
                  ])

    r = client.get("/transactions/stats", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()

    # twd salary +50000, rent -20000 net=30000; billed 全是支出 -1500 -800 -500
    assert body["total_income"] == 50000
    assert body["total_expense"] == 20000 + 1500 + 800 + 500  # = 22800
    assert body["total_net"] == 50000 - 22800

    # per month: 6 月 income 50000 / expense 20000+1500+800 = 22300, net = 27700
    jun = body["amount_by_month"]["2026-06"]
    assert jun["income"] == 50000
    assert jun["expense"] == 20000 + 1500 + 800
    assert jun["net"] == 50000 - (20000 + 1500 + 800)
    assert jun["count"] == 4

    # 5 月只有 billed -500
    may = body["amount_by_month"]["2026-05"]
    assert may["income"] == 0
    assert may["expense"] == 500
    assert may["net"] == -500

    # 月份按 reverse 排（最新在前）
    months = list(body["amount_by_month"].keys())
    assert months == sorted(months, reverse=True)

    # 餐飲類別 1500+800+500 = 2800
    assert body["amount_by_category"]["餐飲"] == 1500 + 800 + 500


def test_transactions_api_exposes_user_cashflow_fields_for_refund(client, data_root):
    """信用卡退稅 raw amount 為負, 但 user cashflow 必須是收入.

    這是 API contract regression: frontend 不該每個頁面各自用 txn_type 重建方向。
    """
    token = _register(client, email="refund-contract@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  billed=[
                      {"card_no": "CARD-1", "date": "2026-06-11", "desc": "GLOBALBLUE TAX REFUND",
                       "amount": -622, "category": "退稅", "txn_type": "refund", "flow_type": "income"},
                      {"card_no": "CARD-1", "date": "2026-06-12", "desc": "DINNER",
                       "amount": 1000, "category": "飲食", "txn_type": "spending", "flow_type": "expense"},
                  ])

    r = client.get("/transactions?bank=hsbc", headers=_auth(token))
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    refund = next(t for t in items if t["description"] == "GLOBALBLUE TAX REFUND")
    assert refund["amount"] == -622          # raw bank/card statement perspective preserved
    assert refund["cashflow_direction"] == "income"
    assert refund["cashflow_amount"] == 622
    assert refund["display_amount"] == 622
    dinner = next(t for t in items if t["description"] == "DINNER")
    assert dinner["amount"] == -1000
    assert dinner["cashflow_direction"] == "expense"
    assert dinner["cashflow_amount"] == 1000
    assert dinner["display_amount"] == 1000

    income = client.get("/transactions?bank=hsbc&direction=income", headers=_auth(token)).json()["items"]
    assert [t["description"] for t in income] == ["GLOBALBLUE TAX REFUND"]
    expense = client.get("/transactions?bank=hsbc&direction=expense", headers=_auth(token)).json()["items"]
    assert [t["description"] for t in expense] == ["DINNER"]

    stats = client.get("/transactions/stats?bank=hsbc", headers=_auth(token)).json()
    assert stats["total_income"] == 622
    assert stats["total_expense"] == 1000
    assert stats["total_net"] == -378
    assert stats["amount_by_month"]["2026-06"]["income"] == 622
    assert stats["amount_by_month"]["2026-06"]["expense"] == 1000


def test_payment_is_neutral_but_preserves_display_magnitude(client, data_root):
    token = _register(client, email="payment-display-contract@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc", pending=[{
        "card_no": "****3254",
        "date": "2026-08-06",
        "desc": "匯豐銀行自動扣款",
        "amount": -12729,
        "txn_type": "payment",
        "flow_type": "transfer",
    }])

    response = client.get("/transactions?bank=hsbc", headers=_auth(token))
    assert response.status_code == 200, response.text
    payment = response.json()["items"][0]
    assert payment["amount"] == -12729
    assert payment["cashflow_direction"] == "neutral"
    assert payment["cashflow_amount"] == 0
    assert payment["display_amount"] == 12729


# ============================================================
# 日期格式正規化 (各家銀行寫入格式不一致)
# 真實 case (2026-06-13 11 家盤點):
#   sinopac twd:    '2026/05/2101:06'  → '2026-05-21'
#   sinopac billed: '2026/05/04'        → '2026-05-04'
#   hsbc:           '2026-06-12'        → '2026-06-12'
#   cathay billed:  '2026-04-08T00:00:00' → '2026-04-08'
# 若不正規化, lexicographic sort 把 sinopac '/' 排到 hsbc '-' 之前
# ============================================================

def test_transactions_normalizes_date_format_and_sorts_correctly(client, data_root):
    """混 / 跟 - 跟 T-suffix 日期, 確認 (a) 正規化吐 ISO YYYY-MM-DD (b) 正確時間倒序."""
    token = _register(client, email="datetest@p.com")
    for bank in ("sinopac", "hsbc", "cathay"):
        r = client.post("/accounts", json={"bank": bank, "label": "test"},
                        headers=_auth(token))
        assert r.status_code == 201

    # sinopac: '/' 分隔, 還黏住時分秒
    _seed_bank_db(data_root, "sinopac",
                  twd=[{"account_no": "S001", "datetime": "2026/06/0319:30",
                        "desc": "sinopac TWD 6/3", "expend": 0, "income": 100}])

    # hsbc: 標準 ISO
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "H****1", "date": "2026-06-12",
                            "desc": "hsbc 6/12", "amount": 100, "scope": "unbilled"}])

    # cathay: ISO + T 時分秒
    _seed_bank_db(data_root, "cathay",
                  billed=[{"card_no": "C****2", "date": "2026-04-08T00:00:00",
                           "desc": "cathay 4/8", "amount": 50, "post_date": "2026-04-10T00:00:00"}])

    r = client.get("/transactions", headers=_auth(token))
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 3

    # 全部統一 'YYYY-MM-DD' (10 字, '-' 分隔, 不含時分秒/T)
    for t in items:
        d = t["date"]
        assert d is not None
        assert len(d) == 10, f"date {d!r} 不是 10 字"
        assert d[4] == "-" and d[7] == "-", f"date {d!r} 不是 ISO"
        assert "/" not in d, f"date {d!r} 還含 '/'"
        assert "T" not in d, f"date {d!r} 還含 'T'"

    # 正確時間倒序: 6/12 (hsbc) > 6/3 (sinopac) > 4/8 (cathay)
    assert items[0]["date"] == "2026-06-12"
    assert items[1]["date"] == "2026-06-03"
    assert items[2]["date"] == "2026-04-08"


# ============================================================
# L8.5: 單筆 detail + PATCH category
# ============================================================

def test_get_transaction_detail_returns_row(client, data_root):
    token = _register(client, email="detail@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "H****1", "date": "2026-06-12",
                            "desc": "test merchant", "amount": 100, "scope": "unbilled"}])

    # 用 list 找出剛存進去的 row id
    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    items = r.json()["items"]
    assert len(items) == 1
    raw_id = items[0]["raw"]["id"]

    # 拿 detail
    r = client.get(f"/transactions/hsbc/pending/{raw_id}", headers=_auth(token))
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["bank"] == "hsbc"
    assert t["kind"] == "pending"
    assert t["description"] == "test merchant"
    assert t["raw"]["id"] == raw_id


def test_get_transaction_detail_404_unknown_id(client, data_root):
    token = _register(client, email="detail404@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12", "desc": "x", "amount": 1}])

    r = client.get("/transactions/hsbc/pending/99999", headers=_auth(token))
    assert r.status_code == 404


def test_get_transaction_detail_403_not_owned_bank(client, data_root):
    token = _register(client, email="detail403@p.com")
    # user 沒加 hsbc account → 即使 db 存在也不該看
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12", "desc": "x", "amount": 1}])

    r = client.get("/transactions/hsbc/pending/1", headers=_auth(token))
    assert r.status_code == 403


def test_get_transaction_detail_400_unknown_kind(client, data_root):
    token = _register(client, email="detailbad@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    r = client.get("/transactions/hsbc/unknown/1", headers=_auth(token))
    assert r.status_code == 422  # Literal validation


def test_patch_transaction_category(client, data_root):
    token = _register(client, email="patch@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12", "desc": "x", "amount": 1}])
    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    raw_id = r.json()["items"][0]["raw"]["id"]

    # PATCH 設 category
    r = client.patch(f"/transactions/hsbc/pending/{raw_id}",
                     json={"category": "餐飲"}, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["category"] == "餐飲"

    # 再 GET 一次確認 persisted
    r = client.get(f"/transactions/hsbc/pending/{raw_id}", headers=_auth(token))
    assert r.json()["category"] == "餐飲"


def test_patch_transaction_clears_dashboard_cache(client, data_root, monkeypatch):
    """交易統計欄位改動後，Dashboard不得繼續拿舊portfolio/stats cache。"""
    from backend.server.routers import transactions as transactions_router

    cleared: list[int] = []
    monkeypatch.setattr(
        transactions_router,
        "clear_dashboard_cache",
        lambda user_id: cleared.append(user_id),
    )
    token = _register(client, email="patch-cache-clear@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc", pending=[
        {"card_no": "X", "date": "2026-06-12", "desc": "x", "amount": 1000},
    ])
    listed = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    raw_id = listed.json()["items"][0]["raw"]["id"]

    response = client.patch(
        f"/transactions/hsbc/pending/{raw_id}",
        json={"auto_excluded": True},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    assert len(cleared) == 1


def test_patch_transaction_clear_category(client, data_root):
    token = _register(client, email="patchclear@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12", "desc": "x", "amount": 1,
                            "category": "餐飲"}])
    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    raw_id = r.json()["items"][0]["raw"]["id"]

    # 空字串應清除
    r = client.patch(f"/transactions/hsbc/pending/{raw_id}",
                     json={"category": ""}, headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["category"] is None


def test_patch_transaction_rejects_unknown_field(client, data_root):
    token = _register(client, email="patchbad@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12", "desc": "x", "amount": 1}])
    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    raw_id = r.json()["items"][0]["raw"]["id"]

    r = client.patch(f"/transactions/hsbc/pending/{raw_id}",
                     json={"amount": 9999}, headers=_auth(token))  # 禁改 amount
    assert r.status_code == 400


def test_patch_transaction_requires_auth(client):
    r = client.patch("/transactions/hsbc/pending/1", json={"category": "x"})
    assert r.status_code == 401


# Phase 8.2 (2026-06-15): PATCH 同時帶 category + subcategory
def test_patch_transaction_with_subcategory(client, data_root):
    token = _register(client, email="patchsubcat@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12", "desc": "x", "amount": 1}])
    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    raw_id = r.json()["items"][0]["raw"]["id"]

    # PATCH 同時設 category + subcategory
    r = client.patch(f"/transactions/hsbc/pending/{raw_id}",
                     json={"category": "飲食", "subcategory": "餐廳"},
                     headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["category"] == "飲食"
    assert r.json()["subcategory"] == "餐廳"

    # 再 GET 一次確認 persisted
    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    item = r.json()["items"][0]
    assert item["category"] == "飲食"
    assert item["subcategory"] == "餐廳"


def test_patch_transaction_subcategory_only(client, data_root):
    """只改 subcategory, category 不動."""
    token = _register(client, email="patchsubonly@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12", "desc": "x", "amount": 1,
                            "category": "飲食"}])
    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    raw_id = r.json()["items"][0]["raw"]["id"]

    r = client.patch(f"/transactions/hsbc/pending/{raw_id}",
                     json={"subcategory": "食品雜貨"},
                     headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["category"] == "飲食"  # 不動
    assert r.json()["subcategory"] == "食品雜貨"


def test_patch_transaction_clear_subcategory(client, data_root):
    """空字串清 subcategory 成 NULL."""
    token = _register(client, email="patchsubclear@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12", "desc": "x", "amount": 1,
                            "category": "飲食"}])
    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    raw_id = r.json()["items"][0]["raw"]["id"]

    # 先設一個 subcategory
    client.patch(f"/transactions/hsbc/pending/{raw_id}",
                 json={"subcategory": "餐廳"}, headers=_auth(token))

    # 再用空字串清掉
    r = client.patch(f"/transactions/hsbc/pending/{raw_id}",
                     json={"subcategory": ""}, headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["subcategory"] is None


# Phase 8.2 (2026-06-14): description_overwrite — 使用者覆寫的說明
def test_patch_transaction_description_overwrite_set(client, data_root):
    """PATCH 設 description_overwrite 後, 回傳 + GET 都帶 overwrite, raw description 不動."""
    token = _register(client, email="descow1@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12",
                            "desc": "JKO TWQR ABCDEFG", "amount": 700}])
    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    raw_id = r.json()["items"][0]["raw"]["id"]

    # PATCH 覆寫
    r = client.patch(f"/transactions/hsbc/pending/{raw_id}",
                     json={"description_overwrite": "蘋果日報訂閱"},
                     headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["description_overwrite"] == "蘋果日報訂閱"
    assert body["description"] == "JKO TWQR ABCDEFG"  # raw 不動 (鐵則)

    # GET 確認 persist
    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    item = r.json()["items"][0]
    assert item["description"] == "JKO TWQR ABCDEFG"
    assert item["description_overwrite"] == "蘋果日報訂閱"


def test_patch_transaction_description_overwrite_clear(client, data_root):
    """空字串清 description_overwrite 成 NULL, raw description 仍然在."""
    token = _register(client, email="descow2@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12",
                            "desc": "RAW_DESC", "amount": 100}])
    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    raw_id = r.json()["items"][0]["raw"]["id"]

    # 先覆寫
    client.patch(f"/transactions/hsbc/pending/{raw_id}",
                 json={"description_overwrite": "MY NOTE"}, headers=_auth(token))

    # 空字串清掉
    r = client.patch(f"/transactions/hsbc/pending/{raw_id}",
                     json={"description_overwrite": ""}, headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["description_overwrite"] is None
    assert body["description"] == "RAW_DESC"  # raw 仍在


def test_patch_transaction_description_with_category(client, data_root):
    """同時改 category + subcategory + description_overwrite — 三欄一起 commit."""
    token = _register(client, email="descow3@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12",
                            "desc": "messy raw", "amount": 100}])
    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    raw_id = r.json()["items"][0]["raw"]["id"]

    r = client.patch(f"/transactions/hsbc/pending/{raw_id}",
                     json={"category": "飲食", "subcategory": "餐廳",
                           "description_overwrite": "清楚的名稱"},
                     headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["category"] == "飲食"
    assert body["subcategory"] == "餐廳"
    assert body["description_overwrite"] == "清楚的名稱"
    assert body["description"] == "messy raw"  # raw 不動


# Phase 8.2 A (2026-06-14): /transactions/stats 帶 filter — chip 來源跟隨 filter
def test_stats_by_category_respects_date_filter(client, data_root):
    """stats since/until 限縮後, by_category 只回該範圍內出現的分類."""
    token = _register(client, email="statsdate@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc", pending=[
        {"card_no": "X", "date": "2026-05-10", "desc": "old", "amount": -100, "category": "飲食"},
        {"card_no": "X", "date": "2026-06-10", "desc": "new", "amount": -200, "category": "交通"},
    ])

    # 不帶 filter — 兩個 category 都出現
    r = client.get("/transactions/stats", headers=_auth(token))
    assert set(r.json()["by_category"].keys()) == {"飲食", "交通"}

    # 限縮 5 月 — 只剩飲食
    r = client.get("/transactions/stats?since=2026-05-01&until=2026-05-31",
                   headers=_auth(token))
    assert set(r.json()["by_category"].keys()) == {"飲食"}

    # 限縮 6 月 — 只剩交通
    r = client.get("/transactions/stats?since=2026-06-01&until=2026-06-30",
                   headers=_auth(token))
    assert set(r.json()["by_category"].keys()) == {"交通"}


def test_stats_by_category_respects_kind_filter(client, data_root):
    """stats kind 限縮後, by_category 只回該 kind 的 row."""
    token = _register(client, email="statskind@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-10", "desc": "p", "amount": -1,
                            "category": "餐飲"}],
                  billed=[{"card_no": "X", "bill_date": "2026-06-01", "date": "2026-05-10",
                           "desc": "b", "amount": -2, "category": "購物"}])

    r = client.get("/transactions/stats?kind=pending", headers=_auth(token))
    assert "餐飲" in r.json()["by_category"] and "購物" not in r.json()["by_category"]

    r = client.get("/transactions/stats?kind=billed", headers=_auth(token))
    assert "購物" in r.json()["by_category"] and "餐飲" not in r.json()["by_category"]


def test_stats_by_subcategory_scoped_to_category(client, data_root):
    """stats 帶 category 只限縮 by_subcategory, by_category 仍回完整 (才有別的主類可切)."""
    token = _register(client, email="statssub@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc", pending=[
        {"card_no": "X", "date": "2026-06-10", "desc": "a", "amount": -1,
         "category": "飲食", "subcategory": "餐廳"},
        {"card_no": "X", "date": "2026-06-10", "desc": "b", "amount": -2,
         "category": "飲食", "subcategory": "食品雜貨"},
        {"card_no": "X", "date": "2026-06-10", "desc": "c", "amount": -3,
         "category": "交通", "subcategory": "大眾運輸"},
    ])

    # 不帶 category — by_subcategory 是全部
    body = client.get("/transactions/stats", headers=_auth(token)).json()
    assert set(body["by_subcategory"].keys()) == {"餐廳", "食品雜貨", "大眾運輸"}
    # 主類 chip 該有兩個
    assert set(body["by_category"].keys()) == {"飲食", "交通"}

    # 帶 category=飲食 — by_subcategory 只剩飲食下的
    body = client.get("/transactions/stats?category=飲食", headers=_auth(token)).json()
    assert set(body["by_subcategory"].keys()) == {"餐廳", "食品雜貨"}
    # 但 by_category 仍回完整 (鐵則: 才有別的主類可切)
    assert set(body["by_category"].keys()) == {"飲食", "交通"}


# Phase 8.2 D (2026-06-14): /rules/categories 過濾 _legacy
def test_rules_categories_excludes_legacy_low_priority(client):
    """priority < 80 的 rule (legacy 降級) 不該出現在 /rules/categories."""
    token = _register(client, email="ruleslegacy@p.com")
    # 建 3 條 rule: 兩條正常 priority=100, 一條 legacy priority=50
    client.post("/rules", json={
        "name": "normal_a", "pattern": "a", "category": "飲食", "priority": 100
    }, headers=_auth(token))
    client.post("/rules", json={
        "name": "normal_b", "pattern": "b", "category": "購物", "priority": 100
    }, headers=_auth(token))
    client.post("/rules", json={
        "name": "old_legacy", "pattern": "x", "category": "帳單", "priority": 50
    }, headers=_auth(token))

    r = client.get("/rules/categories", headers=_auth(token))
    cats = set(r.json()["categories"])
    assert "飲食" in cats and "購物" in cats
    assert "帳單" not in cats  # legacy 過濾掉


# ============================================================
# Phase 6 (B-full) — txn_type 統計行為 regression
# 對應 ubot ****7027 JCB_CB_ARIGATO_10% 案例:
# 銀行給 -1965 (帳單視角), 必須當 cashback 進 income 不歸 expense.
# ============================================================

def test_stats_cashback_goes_to_income_not_expense(client, data_root):
    """cashback row (amount<0) 必須進 income 不進 expense."""
    token = _register(client, email="cashback-stats@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  billed=[
                      # 一般消費 5000
                      {"card_no": "C", "date": "2026-06-10", "desc": "微風信義",
                       "amount": 5000, "txn_type": "spending"},
                      # 三筆 cashback (從帳單視角是負值, 對使用者是 income)
                      {"card_no": "C", "date": "2026-06-11", "desc": "刷卡現金回饋－日本指定商店",
                       "amount": -15, "txn_type": "cashback"},
                      {"card_no": "C", "date": "2026-06-12", "desc": "刷卡現金回饋－吉鶴卡日幣回饋",
                       "amount": -403, "txn_type": "cashback"},
                      {"card_no": "C", "date": "2026-06-13", "desc": "JCB_CB_ARIGATO_10%",
                       "amount": -1965, "txn_type": "cashback"},
                  ])

    r = client.get("/transactions/stats", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()

    # cashback 不應該進 expense — 若舊邏輯 (純看符號) 會是 5000+15+403+1965=7383 灌進 expense
    # 新邏輯: spending 5000 進 expense, cashback 三筆 15+403+1965=2383 進 income
    assert body["total_income"] == 2383, \
        f"cashback 三筆 (15+403+1965) 必須進 income, 實際={body['total_income']}"
    assert body["total_expense"] == 5000, \
        f"只有 spending 5000 進 expense, 實際={body['total_expense']}"
    assert body["total_net"] == 2383 - 5000  # = -2617

    jun = body["amount_by_month"]["2026-06"]
    assert jun["income"] == 2383
    assert jun["expense"] == 5000
    assert jun["net"] == -2617
    assert jun["count"] == 4


def test_stats_refund_goes_to_income(client, data_root):
    """refund 也是正向現金流, 進 income."""
    token = _register(client, email="refund-stats@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  billed=[
                      {"card_no": "C", "date": "2026-06-10", "desc": "消費",
                       "amount": 1000, "txn_type": "spending"},
                      {"card_no": "C", "date": "2026-06-12", "desc": "GetYourGuide 退款",
                       "amount": -300, "txn_type": "refund"},
                  ])
    r = client.get("/transactions/stats", headers=_auth(token))
    body = r.json()
    assert body["total_income"] == 300
    assert body["total_expense"] == 1000


def test_stats_payment_is_transfer_neither_income_nor_expense(client, data_root):
    """payment (還款) 是 transfer, 既不進 income 也不進 expense."""
    token = _register(client, email="payment-stats@p.com")
    client.post("/accounts", json={"bank": "cathay", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "cathay",
                  billed=[
                      {"card_no": "C", "date": "2026-06-10", "desc": "消費",
                       "amount": 1000, "txn_type": "spending"},
                      {"card_no": "C", "date": "2026-06-15", "desc": "本行自動扣繳",
                       "amount": -1000, "txn_type": "payment"},
                  ])
    r = client.get("/transactions/stats", headers=_auth(token))
    body = r.json()
    # payment 跳過, 只算 spending
    assert body["total_income"] == 0
    assert body["total_expense"] == 1000
    jun = body["amount_by_month"]["2026-06"]
    # net 只算 spending 不算 payment
    assert jun["net"] == -1000
    # 但 count 包含 payment
    assert jun["count"] == 2


def test_stats_unknown_txn_type_falls_back_to_sign(client, data_root):
    """txn_type=None 或 unknown 時, 退回舊邏輯純看符號.

    注意: _billed_to_transaction 會把銀行端正值 amt 翻轉成負 (信用卡視角=支出),
    所以這裡 billed 1000 進統計時實際是 -1000 → expense, 不會是 income.
    這是 by-design 的「銀行 billed 從帳單視角→使用者支出視角」翻轉.
    """
    token = _register(client, email="unk-stats@p.com")
    client.post("/accounts", json={"bank": "esun", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "esun",
                  billed=[
                      # billed 1000 → 翻轉成 -1000 進 expense (banked-positive → user-negative)
                      {"card_no": "C", "date": "2026-06-10", "desc": "正值",
                       "amount": 1000},
                      # billed -500 已是負值 → 保留 → expense += 500
                      {"card_no": "C", "date": "2026-06-11", "desc": "負值",
                       "amount": -500},
                  ])
    r = client.get("/transactions/stats", headers=_auth(token))
    body = r.json()
    # 兩筆都進 expense (1000 + 500), income=0
    assert body["total_income"] == 0
    assert body["total_expense"] == 1500


def test_stats_category_excludes_cashback_refund(client, data_root):
    """amount_by_category 只算「真正支出」, cashback/refund 即使有 category 也不進."""
    token = _register(client, email="cat-stats@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  billed=[
                      {"card_no": "C", "date": "2026-06-10", "desc": "餐廳",
                       "amount": 800, "category": "餐飲", "txn_type": "spending"},
                      {"card_no": "C", "date": "2026-06-12", "desc": "回饋",
                       "amount": -100, "category": "餐飲", "txn_type": "cashback"},
                  ])
    r = client.get("/transactions/stats", headers=_auth(token))
    body = r.json()
    # 餐飲類別只算 spending 那筆 800, cashback 不進
    assert body["amount_by_category"]["餐飲"] == 800


def test_transactions_list_exposes_txn_type(client, data_root):
    """GET /transactions 每筆 item 必須帶 txn_type 欄."""
    token = _register(client, email="exp-type@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  billed=[
                      {"card_no": "C", "date": "2026-06-10", "desc": "JCB_CB_ARIGATO",
                       "amount": -1965, "txn_type": "cashback"},
                  ])
    r = client.get("/transactions?bank=ubot", headers=_auth(token))
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["txn_type"] == "cashback"


# ============================================================
# Phase 6 (2026-06-14): excluded flag
# 標 excluded 的帳戶 txn → 仍在 list 裡 (excluded=true), 但 stats 跳過金額
# ============================================================

def test_transactions_list_marks_excluded_account_txns(client, data_root):
    """GET /transactions twd_transactions 該帳戶被標 excluded → item.excluded=true."""
    token = _register(client, email="excl-list@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  twd=[
                      {"account_no": "ACC_IN", "datetime": "2026-06-10T10:00:00",
                       "desc": "薪水", "income": 50000, "category": "薪資"},
                      {"account_no": "ACC_OUT", "datetime": "2026-06-10T11:00:00",
                       "desc": "假測試帳", "expend": 9999, "category": "其他"},
                  ],
                  excluded_accounts=["ACC_OUT"])
    r = client.get("/transactions?bank=ubot", headers=_auth(token))
    items = {it["raw"]["account_no"]: it for it in r.json()["items"]}
    assert items["ACC_IN"]["excluded"] is False
    assert items["ACC_OUT"]["excluded"] is True


def test_transactions_stats_skips_excluded_account_amounts(client, data_root):
    """GET /transactions/stats: excluded 帳戶 txn 金額不進 amount_by_month / by_category / total_*."""
    token = _register(client, email="excl-stats@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  twd=[
                      {"account_no": "ACC_IN", "datetime": "2026-06-10T10:00:00",
                       "desc": "薪水", "income": 50000, "category": "薪資"},
                      {"account_no": "ACC_OUT", "datetime": "2026-06-10T11:00:00",
                       "desc": "排除的支出", "expend": 9999, "category": "其他"},
                  ],
                  excluded_accounts=["ACC_OUT"])
    r = client.get("/transactions/stats?bank=ubot", headers=_auth(token))
    body = r.json()
    # by_kind / by_month / by_bank raw count 仍含兩筆 (含 excluded 的)
    assert body["total"] == 2
    # 金額 bucket 只算 ACC_IN
    assert body["total_income"] == 50000
    assert body["total_expense"] == 0
    jun = body["amount_by_month"]["2026-06"]
    assert jun["income"] == 50000
    assert jun["expense"] == 0
    # amount_by_category 不應該有「其他」(ACC_OUT 的支出 category)
    assert "其他" not in body["amount_by_category"]
    assert body["amount_by_category"] == {}    # 薪資是 income 不算


def test_transactions_stats_card_txn_never_excluded(client, data_root):
    """信用卡走 card_no 不對應 accounts.excluded → 不論 accounts 表怎樣都全算."""
    token = _register(client, email="excl-card@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  billed=[
                      {"card_no": "CARD1", "date": "2026-06-10",
                       "desc": "刷卡", "amount": -1500, "category": "餐飲"},
                  ],
                  # 即使 accounts 表有 excluded, 卡片 txn 不該被影響
                  excluded_accounts=["CARD1"])
    r = client.get("/transactions/stats?bank=ubot", headers=_auth(token))
    body = r.json()
    assert body["total_expense"] == 1500
    assert body["amount_by_category"]["餐飲"] == 1500


# ============================================================
# Phase 6 (2026-06-14 PM): card-level excluded flag
# ============================================================

def test_billed_txn_marks_excluded_card(client, data_root):
    """GET /transactions: 該卡 billed txn 應該帶 excluded=true."""
    token = _register(client, email="excl-billed@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  billed=[
                      {"card_no": "C_IN", "date": "2026-06-10",
                       "desc": "正常卡刷", "amount": -1500, "category": "餐飲"},
                      {"card_no": "C_OUT", "date": "2026-06-10",
                       "desc": "排除卡刷", "amount": -9999, "category": "購物"},
                  ],
                  excluded_cards=["C_OUT"])
    r = client.get("/transactions?bank=ubot", headers=_auth(token))
    items = {it["raw"]["card_no"]: it for it in r.json()["items"]}
    assert items["C_IN"]["excluded"] is False
    assert items["C_OUT"]["excluded"] is True


def test_pending_txn_marks_excluded_card(client, data_root):
    """GET /transactions: 該卡 pending txn 也應該帶 excluded=true."""
    token = _register(client, email="excl-pending@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  pending=[
                      {"card_no": "C_OUT", "date": "2026-06-13",
                       "desc": "排除卡未出帳", "amount": -2000, "category": "購物"},
                  ],
                  excluded_cards=["C_OUT"])
    r = client.get("/transactions?bank=ubot", headers=_auth(token))
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["excluded"] is True


def test_stats_skips_excluded_card_billed_amounts(client, data_root):
    """GET /transactions/stats: excluded 卡 billed txn 金額不算進 amount_by_*."""
    token = _register(client, email="excl-card-stats@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  billed=[
                      {"card_no": "C_IN", "date": "2026-06-10",
                       "desc": "餐廳", "amount": -1500, "category": "餐飲"},
                      {"card_no": "C_OUT", "date": "2026-06-10",
                       "desc": "排除", "amount": -9999, "category": "購物"},
                  ],
                  excluded_cards=["C_OUT"])
    r = client.get("/transactions/stats?bank=ubot", headers=_auth(token))
    body = r.json()
    # raw count 仍含兩筆
    assert body["total"] == 2
    # 金額只算 C_IN, C_OUT 跳過
    assert body["total_expense"] == 1500
    assert body["amount_by_category"]["餐飲"] == 1500
    assert "購物" not in body["amount_by_category"]


# ============================================================
# Phase 6 (2026-06-14 PM): direction query param (收入/支出/全部)
# ============================================================

def test_direction_income_filter(client, data_root):
    """GET /transactions?direction=income → 只回 amount > 0."""
    token = _register(client, email="dir-income@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  twd=[
                      {"account_no": "A", "datetime": "2026-06-10T10:00:00",
                       "desc": "薪水", "income": 50000},
                      {"account_no": "A", "datetime": "2026-06-10T11:00:00",
                       "desc": "電費", "expend": 500},
                  ])
    r = client.get("/transactions?bank=ubot&direction=income", headers=_auth(token))
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["amount"] == 50000


def test_direction_expense_filter(client, data_root):
    """GET /transactions?direction=expense → 只回 amount < 0."""
    token = _register(client, email="dir-expense@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  twd=[
                      {"account_no": "A", "datetime": "2026-06-10T10:00:00",
                       "desc": "薪水", "income": 50000},
                      {"account_no": "A", "datetime": "2026-06-10T11:00:00",
                       "desc": "電費", "expend": 500},
                  ],
                  billed=[
                      {"card_no": "C", "date": "2026-06-09", "desc": "餐廳",
                       "amount": -1500},
                  ])
    r = client.get("/transactions?bank=ubot&direction=expense", headers=_auth(token))
    items = r.json()["items"]
    # 電費 -500 + 餐廳 -1500
    assert len(items) == 2
    assert all(it["amount"] < 0 for it in items)


def test_direction_all_default_unchanged(client, data_root):
    """GET /transactions (沒帶 direction) → 等於 direction=all 全收."""
    token = _register(client, email="dir-default@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  twd=[
                      {"account_no": "A", "datetime": "2026-06-10T10:00:00",
                       "desc": "薪水", "income": 50000},
                      {"account_no": "A", "datetime": "2026-06-10T11:00:00",
                       "desc": "電費", "expend": 500},
                  ])
    r_default = client.get("/transactions?bank=ubot", headers=_auth(token))
    r_all = client.get("/transactions?bank=ubot&direction=all", headers=_auth(token))
    assert r_default.json()["total"] == r_all.json()["total"] == 2


# ============================================================
# Phase 6 (category taxonomy 2026-06-15) — stats 新欄位:
#   amount_by_flow_type / subscription_total / subscription_by_month
# ============================================================

def test_stats_amount_by_flow_type_buckets(client, data_root):
    """stats 回傳 amount_by_flow_type 4 桶 (expense/income/transfer/investment).
    跟既有 txn_type 邏輯解耦, 純看 flow_type 欄.
    """
    token = _register(client, email="flow-type-buckets@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  twd=[
                      # expense 1500
                      {"account_no": "A", "datetime": "2026-06-10 09:00",
                       "desc": "餐廳", "expend": 1500, "flow_type": "expense"},
                      # income 50000
                      {"account_no": "A", "datetime": "2026-06-10 10:00",
                       "desc": "薪水", "income": 50000, "flow_type": "income"},
                      # transfer 20000
                      {"account_no": "A", "datetime": "2026-06-10 11:00",
                       "desc": "繳信用卡費", "expend": 20000, "flow_type": "transfer"},
                      # investment 30000
                      {"account_no": "A", "datetime": "2026-06-10 12:00",
                       "desc": "申購基金 元大台灣 50", "expend": 30000, "flow_type": "investment"},
                  ])

    r = client.get("/transactions/stats", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()

    flow = body.get("amount_by_flow_type")
    assert flow is not None, "stats 必須吐 amount_by_flow_type"
    assert flow["expense"] == 1500
    assert flow["income"] == 50000
    assert flow["transfer"] == 20000
    assert flow["investment"] == 30000


def test_stats_subscription_total_aggregate(client, data_root):
    """is_subscription=1 的 expense txn 加總進 subscription_total."""
    token = _register(client, email="sub-total@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  billed=[
                      # Netflix 390 (is_subscription=1)
                      {"card_no": "C", "date": "2026-06-05", "desc": "NETFLIX.COM",
                       "amount": 390, "txn_type": "spending", "flow_type": "expense",
                       "is_subscription": True},
                      # iCloud 30 (is_subscription=1)
                      {"card_no": "C", "date": "2026-06-10", "desc": "APPLE.COM/BILL iCloud+",
                       "amount": 30, "txn_type": "spending", "flow_type": "expense",
                       "is_subscription": True},
                      # 一般消費 1200 (is_subscription=0)
                      {"card_no": "C", "date": "2026-06-15", "desc": "全家便利商店",
                       "amount": 1200, "txn_type": "spending", "flow_type": "expense"},
                  ])

    r = client.get("/transactions/stats", headers=_auth(token))
    body = r.json()

    # 信用卡 amount 正值 = 銀行視角消費, 對使用者是負支出 (反號)
    # subscription_total 算的是 abs(amt), 對 expense row 應是 390+30=420
    assert body.get("subscription_total") == 420, \
        f"訂閱合計應 420 (Netflix 390 + iCloud 30), 實際 {body.get('subscription_total')}"
    # 一般消費 1200 不算訂閱
    by_month = body.get("subscription_by_month", {})
    assert by_month.get("2026-06") == 420


def test_stats_subscription_zero_when_no_subs(client, data_root):
    """沒訂閱 row 時 subscription_total=0, subscription_by_month={}."""
    token = _register(client, email="no-sub@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  billed=[
                      {"card_no": "C", "date": "2026-06-15", "desc": "全家",
                       "amount": 100, "txn_type": "spending", "flow_type": "expense"},
                  ])
    r = client.get("/transactions/stats", headers=_auth(token))
    body = r.json()
    assert body.get("subscription_total") == 0
    assert body.get("subscription_by_month") == {}


def test_stats_flow_type_excluded_account_skipped(client, data_root):
    """excluded 帳戶的 flow_type 不該進 amount_by_flow_type."""
    token = _register(client, email="flow-excl@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  twd=[
                      # 列入統計的 row
                      {"account_no": "A1", "datetime": "2026-06-10 09:00",
                       "desc": "薪水", "income": 30000, "flow_type": "income"},
                      # 排除的 row
                      {"account_no": "A_EXCL", "datetime": "2026-06-10 10:00",
                       "desc": "排除帳戶薪水", "income": 99999, "flow_type": "income"},
                  ],
                  excluded_accounts=["A_EXCL"])

    r = client.get("/transactions/stats", headers=_auth(token))
    body = r.json()
    flow = body.get("amount_by_flow_type")
    # 只算 A1 的 30000, 不算 A_EXCL 的 99999
    assert flow["income"] == 30000


# ============================================================
# Phase 7 (Income 5 類 2026-06-15) — stats 新欄位:
#   amount_by_income_category / passive_income_total / passive_income_by_month
#   passive_income_pct / income_unclassified_count
# ============================================================

def test_stats_amount_by_income_category_5_buckets(client, data_root):
    """stats 回傳 amount_by_income_category 5 桶 (salary/bonus/interest_dividend/investment_gain/other).

    只計 flow_type='income' 的 row, 信用卡 refund/cashback 即使 flow_type=income
    但 income_category=None 不算進 5 類 (但累計 unclassified_count).
    """
    token = _register(client, email="ic-buckets@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  twd=[
                      {"account_no": "1", "datetime": "2026-06-01 09:00",
                       "desc": "薪資匯入 6 月", "income": 80000,
                       "flow_type": "income", "income_category": "salary"},
                      {"account_no": "1", "datetime": "2026-06-15 09:00",
                       "desc": "年終獎金", "income": 50000,
                       "flow_type": "income", "income_category": "bonus"},
                      {"account_no": "1", "datetime": "2026-06-20 09:00",
                       "desc": "利息存入", "income": 907,
                       "flow_type": "income", "income_category": "interest_dividend"},
                      {"account_no": "1", "datetime": "2026-06-25 09:00",
                       "desc": "證券交割款", "income": 5000,
                       "flow_type": "income", "income_category": "investment_gain"},
                      {"account_no": "1", "datetime": "2026-06-28 09:00",
                       "desc": "退稅入帳", "income": 2000,
                       "flow_type": "income", "income_category": "other"},
                      # Edge: unclassified income (income_category=None)
                      {"account_no": "1", "datetime": "2026-06-30 09:00",
                       "desc": "ATM 匯款 (unknown source)", "income": 1500,
                       "flow_type": "income", "income_category": None},
                  ])

    r = client.get("/transactions/stats", headers=_auth(token))
    body = r.json()
    ic = body.get("amount_by_income_category")
    assert ic is not None, "stats 必須吐 amount_by_income_category"
    assert ic["salary"] == 80000
    assert ic["bonus"] == 50000
    assert ic["interest_dividend"] == 907
    assert ic["investment_gain"] == 5000
    assert ic["other"] == 2000
    # unclassified income 累計 (1500 不算進 5 類)
    assert body.get("income_unclassified_count") == 1


def test_stats_passive_income_total_is_interest_plus_invest(client, data_root):
    """passive_income_total = interest_dividend + investment_gain (FIRE 公式分子)."""
    token = _register(client, email="passive-total@p.com")
    client.post("/accounts", json={"bank": "sinopac", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "sinopac",
                  twd=[
                      {"account_no": "1", "datetime": "2026-06-01 09:00",
                       "desc": "利息存入", "income": 907,
                       "flow_type": "income", "income_category": "interest_dividend"},
                      {"account_no": "1", "datetime": "2026-06-15 09:00",
                       "desc": "股息 2330", "income": 3000,
                       "flow_type": "income", "income_category": "interest_dividend"},
                      {"account_no": "1", "datetime": "2026-06-20 09:00",
                       "desc": "證券交割款", "income": 12000,
                       "flow_type": "income", "income_category": "investment_gain"},
                      # 主動收入不算進 passive
                      {"account_no": "1", "datetime": "2026-06-25 09:00",
                       "desc": "薪資匯入", "income": 60000,
                       "flow_type": "income", "income_category": "salary"},
                      {"account_no": "1", "datetime": "2026-06-28 09:00",
                       "desc": "獎金", "income": 5000,
                       "flow_type": "income", "income_category": "bonus"},
                  ])

    r = client.get("/transactions/stats", headers=_auth(token))
    body = r.json()
    # passive = 907 + 3000 + 12000 = 15907 (interest_dividend + investment_gain)
    assert body.get("passive_income_total") == 15907


def test_stats_rejects_stale_income_category_on_expense_row(client, data_root):
    """Persisted income 標籤若與使用者視角方向衝突，不得進收入或 FIRE 分子。"""
    token = _register(client, email="passive-direction@p.com")
    client.post("/accounts", json={"bank": "scsb", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "scsb", twd=[
        {"account_no": "1", "datetime": "2026-07-24 09:00",
         "desc": "放款利息", "expend": 38395,
         "flow_type": "income", "income_category": "interest_dividend"},
        {"account_no": "1", "datetime": "2026-07-25 09:00",
         "desc": "存款利息", "income": 31,
         "flow_type": "income", "income_category": "interest_dividend"},
    ])

    body = client.get("/transactions/stats", headers=_auth(token)).json()

    assert body["amount_by_income_category"]["interest_dividend"] == 31
    assert body["passive_income_total"] == 31
    assert body["passive_income_by_month"] == {"2026-07": 31}


def test_stats_passive_income_pct_calculated(client, data_root):
    """passive_income_pct = passive_income / total_income * 100 (1 位小數)."""
    token = _register(client, email="passive-pct@p.com")
    client.post("/accounts", json={"bank": "sinopac", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "sinopac",
                  twd=[
                      # 主動 80000, 被動 20000, total 100000, passive % = 20.0
                      {"account_no": "1", "datetime": "2026-06-01 09:00",
                       "desc": "薪資", "income": 80000,
                       "flow_type": "income", "income_category": "salary"},
                      {"account_no": "1", "datetime": "2026-06-15 09:00",
                       "desc": "股息 2330", "income": 15000,
                       "flow_type": "income", "income_category": "interest_dividend"},
                      {"account_no": "1", "datetime": "2026-06-20 09:00",
                       "desc": "證券處分", "income": 5000,
                       "flow_type": "income", "income_category": "investment_gain"},
                  ])

    r = client.get("/transactions/stats", headers=_auth(token))
    body = r.json()
    assert body.get("passive_income_total") == 20000
    # total_income (subscript: total_income 沿 amount_by_month 算; 全 income 100000)
    assert body.get("total_income") == 100000
    assert body.get("passive_income_pct") == 20.0


def test_stats_passive_income_pct_zero_when_no_income(client, data_root):
    """total_income=0 時 passive_income_pct=0 (不該 ZeroDivisionError)."""
    token = _register(client, email="passive-zero@p.com")
    client.post("/accounts", json={"bank": "sinopac", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "sinopac",
                  twd=[
                      {"account_no": "1", "datetime": "2026-06-01 09:00",
                       "desc": "餐飲", "expend": 200, "flow_type": "expense"},
                  ])

    r = client.get("/transactions/stats", headers=_auth(token))
    body = r.json()
    assert body.get("passive_income_total") == 0
    assert body.get("passive_income_pct") == 0.0


def test_stats_passive_income_by_month_sorted_desc(client, data_root):
    """passive_income_by_month 按月分桶 (用於 6 月趨勢 sparkline)."""
    token = _register(client, email="passive-month@p.com")
    client.post("/accounts", json={"bank": "sinopac", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "sinopac",
                  twd=[
                      {"account_no": "1", "datetime": "2026-04-15 09:00",
                       "desc": "股息 2330", "income": 1000,
                       "flow_type": "income", "income_category": "interest_dividend"},
                      {"account_no": "1", "datetime": "2026-05-15 09:00",
                       "desc": "股息 2330", "income": 1500,
                       "flow_type": "income", "income_category": "interest_dividend"},
                      {"account_no": "1", "datetime": "2026-06-15 09:00",
                       "desc": "證券交割", "income": 2000,
                       "flow_type": "income", "income_category": "investment_gain"},
                  ])

    r = client.get("/transactions/stats", headers=_auth(token))
    body = r.json()
    by_month = body.get("passive_income_by_month", {})
    assert by_month == {"2026-06": 2000, "2026-05": 1500, "2026-04": 1000}
    # 確認 desc 排序 (最新月在前 — sparkline 取最近 6 個月)
    months = list(by_month.keys())
    assert months == sorted(months, reverse=True)


def test_stats_income_category_excluded_account_skipped(client, data_root):
    """excluded 帳戶的 income_category 不該進 amount_by_income_category."""
    token = _register(client, email="ic-excl@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_bank_db(data_root, "ubot",
                  twd=[
                      {"account_no": "A1", "datetime": "2026-06-10 09:00",
                       "desc": "薪資", "income": 50000,
                       "flow_type": "income", "income_category": "salary"},
                      {"account_no": "A_EXCL", "datetime": "2026-06-10 10:00",
                       "desc": "排除帳戶利息", "income": 99999,
                       "flow_type": "income", "income_category": "interest_dividend"},
                  ],
                  excluded_accounts=["A_EXCL"])

    r = client.get("/transactions/stats", headers=_auth(token))
    body = r.json()
    ic = body.get("amount_by_income_category")
    assert ic["salary"] == 50000
    # excluded 帳戶 99999 利息不算
    assert ic["interest_dividend"] == 0
    assert body.get("passive_income_total") == 0


# ============================================================
# Phase 8.2 B (2026-06-14): 未分類 chip — __null__ sentinel
# ============================================================
def test_stats_by_category_includes_null_sentinel(client, data_root):
    """category IS NULL/"" 的 row 用 __null__ key 暴露給 frontend chip."""
    token = _register(client, email="statsnull@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc", pending=[
        {"card_no": "X", "date": "2026-06-10", "desc": "classified", "amount": -100, "category": "飲食"},
        {"card_no": "X", "date": "2026-06-11", "desc": "no-cat", "amount": -200},  # category 預設 NULL
        {"card_no": "X", "date": "2026-06-12", "desc": "empty-cat", "amount": -300, "category": ""},
    ])

    r = client.get("/transactions/stats", headers=_auth(token))
    body = r.json()
    # __null__ chip 出現 — 2 row (NULL + "")
    assert body["by_category"].get("__null__") == 2
    assert body["by_category"].get("飲食") == 1


def test_transactions_filter_by_null_sentinel(client, data_root):
    """/transactions?category=__null__ 篩出 NULL/空字串 category row."""
    token = _register(client, email="txnnull@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc", pending=[
        {"card_no": "X", "date": "2026-06-10", "desc": "classified", "amount": -100, "category": "飲食"},
        {"card_no": "X", "date": "2026-06-11", "desc": "no-cat-1", "amount": -200},
        {"card_no": "X", "date": "2026-06-12", "desc": "no-cat-2", "amount": -300, "category": ""},
    ])

    r = client.get("/transactions?category=__null__", headers=_auth(token))
    items = r.json()["items"]
    assert len(items) == 2
    assert all(t["category"] in (None, "") for t in items)

    # 對照組: category=飲食 只回 1 筆
    r = client.get("/transactions?category=飲食", headers=_auth(token))
    assert len(r.json()["items"]) == 1


# ============================================================
# Phase 8.3 (2026-06-15) — auto_excluded 自動排除收支
# ============================================================

def test_transactions_exposes_auto_excluded(client, data_root):
    """transform 暴露 auto_excluded 旗標, frontend 才能反灰顯示."""
    token = _register(client, email="autoex1@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc", billed=[
        {"card_no": "X", "date": "2026-06-10", "desc": "刷卡 1000",
         "amount": 1000, "category": "飲食"},
        {"card_no": "X", "date": "2026-06-11", "desc": "匯豐銀行自動扣款",
         "amount": -1000, "category": "還款",
         "txn_type": "payment", "auto_excluded": True},
    ])
    r = client.get("/transactions?bank=hsbc", headers=_auth(token))
    items = sorted(r.json()["items"], key=lambda t: t["date"])
    assert items[0]["auto_excluded"] is False
    assert items[1]["auto_excluded"] is True


def test_stats_skips_auto_excluded_amounts(client, data_root):
    """stats: auto_excluded=True 的 row 不算進 income/expense/transfer/by_category."""
    token = _register(client, email="autoex2@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc", billed=[
        # 一般刷卡 1000 → expense
        {"card_no": "X", "date": "2026-06-10", "desc": "餐廳 1000",
         "amount": 1000, "category": "飲食"},
        # 還款 -7137 → auto_excluded, 不算 stats
        {"card_no": "X", "date": "2026-06-05", "desc": "匯豐銀行自動扣款",
         "amount": -7137, "category": "還款",
         "txn_type": "payment", "flow_type": "transfer",
         "auto_excluded": True},
    ])
    r = client.get("/transactions/stats?bank=hsbc", headers=_auth(token))
    body = r.json()
    # 1. 收支桶: 只算第一筆 1000
    assert body["total_expense"] == 1000, f"expense should = 1000 (auto_excluded skipped), got {body['total_expense']}"
    assert body["total_income"] == 0
    # 2. flow_type transfer 桶: 還款 7137 不入 transfer
    assert body["amount_by_flow_type"]["transfer"] == 0, "auto_excluded row should not count toward transfer flow_type"
    assert body["amount_by_flow_type"]["expense"] == 1000
    # 3. by_category chip count: 只算飲食 1, 還款 row 不入 chip
    assert body["by_category"] == {"飲食": 1}, f"還款 chip should be hidden, got {body['by_category']}"
    # 4. amount_by_category: 飲食 1000
    assert body["amount_by_category"]["飲食"] == 1000


def test_stats_excluded_and_auto_excluded_are_or(client, data_root):
    """既有 per-account excluded 跟 auto_excluded 是 OR 邏輯 (任一 true 都 skip)."""
    token = _register(client, email="autoex3@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
        twd=[
            # 普通 row (進 stats)
            {"account_no": "111", "datetime": "2026-06-10T10:00:00", "desc": "薪資",
             "income": 50000, "category": "薪資", "flow_type": "income"},
            # excluded account row (per-account excluded)
            {"account_no": "999", "datetime": "2026-06-11T10:00:00", "desc": "舊帳戶利息",
             "income": 100, "category": "利息股息"},
            # auto_excluded row (rule 命中)
            {"account_no": "111", "datetime": "2026-06-12T10:00:00", "desc": "轉帳出去",
             "expend": 3000, "category": "轉帳", "flow_type": "transfer",
             "auto_excluded": True},
        ],
        excluded_accounts=["999"],
    )
    r = client.get("/transactions/stats?bank=hsbc", headers=_auth(token))
    body = r.json()
    # 只第一筆 50000 進 income
    assert body["total_income"] == 50000
    # transfer 桶不算 (auto_excluded skip)
    assert body["amount_by_flow_type"]["transfer"] == 0
    # by_category 只有薪資, 沒有「利息股息」(per-account excluded) 也沒有「轉帳」(auto_excluded)
    assert body["by_category"] == {"薪資": 1}

# ---------------------------------------------------------------------------
# Phase 9 (2026-06-16) — tags_overwrite: user 自定義標籤 (JSON array)
# 跟 description_overwrite 同 overlay pattern: raw 不動, tags 純 user 自加.
# ---------------------------------------------------------------------------


def test_tags_default_empty_list_when_not_set(client, data_root):
    """新 row tags 欄沒被動過 → GET 回 [] (不是 None)."""
    token = _register(client, email="tags1@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12",
                            "desc": "coffee", "amount": 120}])
    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    item = r.json()["items"][0]
    assert item["tags"] == []


def test_patch_tags_set(client, data_root):
    """PATCH 設 tags 後, 回傳 + GET 都帶完整 tag list."""
    token = _register(client, email="tags2@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12",
                            "desc": "東京迪士尼", "amount": 5800}])
    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    raw_id = r.json()["items"][0]["raw"]["id"]

    r = client.patch(f"/transactions/hsbc/pending/{raw_id}",
                     json={"tags": ["日本旅遊", "迪士尼", "2026"]},
                     headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tags"] == ["日本旅遊", "迪士尼", "2026"]

    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    assert r.json()["items"][0]["tags"] == ["日本旅遊", "迪士尼", "2026"]


def test_patch_tags_clear_with_empty_list(client, data_root):
    """空 list 清掉 tags, raw 仍在."""
    token = _register(client, email="tags3@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12",
                            "desc": "raw_desc", "amount": 100}])
    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    raw_id = r.json()["items"][0]["raw"]["id"]

    # 先標
    client.patch(f"/transactions/hsbc/pending/{raw_id}",
                 json={"tags": ["a", "b"]}, headers=_auth(token))

    # 空 list 清掉
    r = client.patch(f"/transactions/hsbc/pending/{raw_id}",
                     json={"tags": []}, headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["tags"] == []
    assert body["description"] == "raw_desc"  # raw 仍在


def test_patch_tags_dedupe_and_strip(client, data_root):
    """重複 tag / 前後空白 / 空字串 → normalize 過濾乾淨, 保留首次出現順序."""
    token = _register(client, email="tags4@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12",
                            "desc": "d", "amount": 100}])
    raw_id = client.get("/transactions?bank=hsbc&kind=pending",
                        headers=_auth(token)).json()["items"][0]["raw"]["id"]

    r = client.patch(f"/transactions/hsbc/pending/{raw_id}",
                     json={"tags": ["  週末  ", "週末", "", "出差", "出差"]},
                     headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["tags"] == ["週末", "出差"]


def test_patch_tags_rejects_non_string(client, data_root):
    """tags 非字串 (e.g. 數字) → 400."""
    token = _register(client, email="tags5@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12",
                            "desc": "d", "amount": 100}])
    raw_id = client.get("/transactions?bank=hsbc&kind=pending",
                        headers=_auth(token)).json()["items"][0]["raw"]["id"]

    r = client.patch(f"/transactions/hsbc/pending/{raw_id}",
                     json={"tags": ["ok", 123]}, headers=_auth(token))
    assert r.status_code == 400
    assert "字串" in r.json()["detail"]


def test_patch_tags_rejects_oversize_tag(client, data_root):
    """單個 tag 超過 50 字 → 400."""
    token = _register(client, email="tags6@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12",
                            "desc": "d", "amount": 100}])
    raw_id = client.get("/transactions?bank=hsbc&kind=pending",
                        headers=_auth(token)).json()["items"][0]["raw"]["id"]

    long_tag = "x" * 51
    r = client.patch(f"/transactions/hsbc/pending/{raw_id}",
                     json={"tags": [long_tag]}, headers=_auth(token))
    assert r.status_code == 400
    assert "標籤過長" in r.json()["detail"]


def test_patch_tags_combined_with_category(client, data_root):
    """同 PATCH 改 category + subcategory + tags — 三欄一起 commit."""
    token = _register(client, email="tags7@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12",
                            "desc": "東京晚餐", "amount": 1200}])
    raw_id = client.get("/transactions?bank=hsbc&kind=pending",
                        headers=_auth(token)).json()["items"][0]["raw"]["id"]

    r = client.patch(f"/transactions/hsbc/pending/{raw_id}",
                     json={"category": "飲食", "subcategory": "餐廳",
                           "tags": ["日本旅遊"]},
                     headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["category"] == "飲食"
    assert body["subcategory"] == "餐廳"
    assert body["tags"] == ["日本旅遊"]


# ---------------------------------------------------------------------------
# Phase 9.1 (2026-06-17) — /transactions/tags/popular: tag picker source
# 跨 11 家 SQLite + 3 表 aggregate tags_overwrite, 給 frontend picker 用.
# ---------------------------------------------------------------------------


def test_tags_popular_empty_when_no_tags(client, data_root):
    """新 user 完全沒標過 tag → 回 {tags: []}."""
    token = _register(client, email="popular0@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12",
                            "desc": "coffee", "amount": 120}])
    r = client.get("/transactions/tags/popular", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json() == {"tags": []}


def test_tags_popular_count_aggregates_across_tables(client, data_root):
    """同名 tag 跨 3 表 + 多銀行 count 加總, 預設 by count desc."""
    token = _register(client, email="popular1@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "h"}, headers=_auth(token))
    client.post("/accounts", json={"bank": "cathay", "label": "c"}, headers=_auth(token))
    # hsbc: pending 一筆標「日本旅遊」, billed 一筆標「日本旅遊」「迪士尼」
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12",
                            "desc": "東京晚餐", "amount": 1200}],
                  billed=[{"card_no": "X", "date": "2026-05-01",
                           "desc": "迪士尼門票", "amount": 5800}])
    # cathay: twd 一筆標「日本旅遊」「機票」
    _seed_bank_db(data_root, "cathay",
                  twd=[{"account_no": "A", "datetime": "2026-04-15 09:00",
                        "desc": "華航機票", "expend": 32000}])

    # hsbc pending 標
    pid = client.get("/transactions?bank=hsbc&kind=pending",
                     headers=_auth(token)).json()["items"][0]["raw"]["id"]
    client.patch(f"/transactions/hsbc/pending/{pid}",
                 json={"tags": ["日本旅遊"]}, headers=_auth(token))
    # hsbc billed 標
    bid = client.get("/transactions?bank=hsbc&kind=billed",
                     headers=_auth(token)).json()["items"][0]["raw"]["id"]
    client.patch(f"/transactions/hsbc/billed/{bid}",
                 json={"tags": ["日本旅遊", "迪士尼"]}, headers=_auth(token))
    # cathay twd 標
    tid = client.get("/transactions?bank=cathay&kind=twd",
                     headers=_auth(token)).json()["items"][0]["raw"]["id"]
    client.patch(f"/transactions/cathay/twd/{tid}",
                 json={"tags": ["日本旅遊", "機票"]}, headers=_auth(token))

    r = client.get("/transactions/tags/popular", headers=_auth(token))
    assert r.status_code == 200, r.text
    tags = r.json()["tags"]
    # 預設 sort by count desc; 日本旅遊 3 > 迪士尼/機票 1
    names = [t["name"] for t in tags]
    assert names[0] == "日本旅遊"
    assert set(names) == {"日本旅遊", "迪士尼", "機票"}
    # 不暴露 count 給 UI 也 OK, 但 API 留欄位給 ranking debug
    counts = {t["name"]: t["count"] for t in tags}
    assert counts["日本旅遊"] == 3
    assert counts["迪士尼"] == 1
    assert counts["機票"] == 1


def test_tags_popular_sort_recent_uses_last_used(client, data_root):
    """sort=recent: by 該 tag 最後一次被掛上的 row 的 date desc."""
    token = _register(client, email="popular2@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "h"}, headers=_auth(token))
    # 三筆不同日, 標不同 tag — count 都=1, 但 recent 順序看 date
    _seed_bank_db(data_root, "hsbc",
                  pending=[
                      {"card_no": "X", "date": "2026-04-01",
                       "desc": "舊", "amount": 100},
                      {"card_no": "X", "date": "2026-06-15",
                       "desc": "新", "amount": 200},
                      {"card_no": "X", "date": "2026-05-10",
                       "desc": "中", "amount": 300},
                  ])
    items = client.get("/transactions?bank=hsbc&kind=pending",
                       headers=_auth(token)).json()["items"]
    # 依 desc 排回去, 找到對應 id 標
    by_desc = {it["raw"]["description"]: it["raw"]["id"] for it in items}
    client.patch(f"/transactions/hsbc/pending/{by_desc['舊']}",
                 json={"tags": ["A"]}, headers=_auth(token))
    client.patch(f"/transactions/hsbc/pending/{by_desc['新']}",
                 json={"tags": ["B"]}, headers=_auth(token))
    client.patch(f"/transactions/hsbc/pending/{by_desc['中']}",
                 json={"tags": ["C"]}, headers=_auth(token))

    # default sort=count → 3 tag count 都 1, 順序未定義
    # sort=recent → B(6/15) > C(5/10) > A(4/01)
    r = client.get("/transactions/tags/popular?sort=recent", headers=_auth(token))
    assert r.status_code == 200, r.text
    names = [t["name"] for t in r.json()["tags"]]
    assert names == ["B", "C", "A"]


def test_tags_popular_rejects_invalid_sort(client):
    """sort 只接受 count / recent."""
    token = _register(client, email="popular3@p.com")
    r = client.get("/transactions/tags/popular?sort=foo", headers=_auth(token))
    assert r.status_code in (400, 422)


def test_tags_popular_requires_auth(client):
    r = client.get("/transactions/tags/popular")
    assert r.status_code == 401


def test_rename_hashtag_updates_all_transactions_and_merges_duplicates(client, data_root):
    token = _register(client, email="tag-rename@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(
        data_root,
        "hsbc",
        pending=[
            {"card_no": "X", "date": "2026-07-19", "desc": "東京", "amount": 100},
            {"card_no": "X", "date": "2026-07-20", "desc": "大阪", "amount": 200},
        ],
    )
    items = client.get(
        "/transactions?bank=hsbc&kind=pending", headers=_auth(token),
    ).json()["items"]
    by_desc = {item["description"]: item["raw"]["id"] for item in items}
    client.patch(
        f"/transactions/hsbc/pending/{by_desc['東京']}",
        json={"tags": ["日本旅遊", "家庭"]}, headers=_auth(token),
    )
    client.patch(
        f"/transactions/hsbc/pending/{by_desc['大阪']}",
        json={"tags": ["旅行", "日本旅遊"]}, headers=_auth(token),
    )

    response = client.put(
        "/transactions/tags",
        json={"old_name": "日本旅遊", "name": "旅行"},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["transactions_updated"] == 2
    items = client.get(
        "/transactions?bank=hsbc&kind=pending", headers=_auth(token),
    ).json()["items"]
    tags_by_desc = {item["description"]: item["tags"] for item in items}
    assert tags_by_desc["東京"] == ["旅行", "家庭"]
    assert tags_by_desc["大阪"] == ["旅行"]
    names = [
        item["name"]
        for item in client.get("/transactions/tags/popular", headers=_auth(token)).json()["tags"]
    ]
    assert "日本旅遊" not in names
    assert "旅行" in names


def test_delete_hashtag_removes_only_that_hashtag(client, data_root):
    token = _register(client, email="tag-delete@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(
        data_root,
        "hsbc",
        pending=[{"card_no": "X", "date": "2026-07-20", "desc": "東京", "amount": 100}],
    )
    item = client.get(
        "/transactions?bank=hsbc&kind=pending", headers=_auth(token),
    ).json()["items"][0]
    client.patch(
        f"/transactions/hsbc/pending/{item['raw']['id']}",
        json={"tags": ["日本旅遊", "家庭"]}, headers=_auth(token),
    )

    response = client.request(
        "DELETE", "/transactions/tags",
        json={"name": "日本旅遊"}, headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["transactions_updated"] == 1
    item = client.get(
        "/transactions?bank=hsbc&kind=pending", headers=_auth(token),
    ).json()["items"][0]
    assert item["tags"] == ["家庭"]



# ---------------------------------------------------------------------------
# Phase 9.2 (2026-06-17) — single PATCH 加 tags_mode 支援 (replace / add)
# 給 bulk edit 用: frontend 對 N 筆 Promise.all 連發 single PATCH, 不開新 endpoint.
# ---------------------------------------------------------------------------


def test_patch_tags_mode_replace_default(client, data_root):
    """tags_mode 預設 replace — 老行為 (沒 mode 也算 replace)."""
    token = _register(client, email="mode1@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "h"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12",
                            "desc": "x", "amount": 100}])
    rid = client.get("/transactions?bank=hsbc&kind=pending",
                     headers=_auth(token)).json()["items"][0]["raw"]["id"]
    # 先設舊
    client.patch(f"/transactions/hsbc/pending/{rid}",
                 json={"tags": ["舊"]}, headers=_auth(token))
    # 不給 tags_mode → 預設 replace
    r = client.patch(f"/transactions/hsbc/pending/{rid}",
                     json={"tags": ["新"]}, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["tags"] == ["新"]
    # 顯式給 replace 也一樣
    r = client.patch(f"/transactions/hsbc/pending/{rid}",
                     json={"tags": ["X", "Y"], "tags_mode": "replace"},
                     headers=_auth(token))
    assert r.json()["tags"] == ["X", "Y"]


def test_patch_tags_mode_add_merges_dedupe(client, data_root):
    """tags_mode='add' 跟現有 merge + dedup 大小寫不敏感, 保留現有順序."""
    token = _register(client, email="mode2@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "h"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12",
                            "desc": "x", "amount": 100}])
    rid = client.get("/transactions?bank=hsbc&kind=pending",
                     headers=_auth(token)).json()["items"][0]["raw"]["id"]
    client.patch(f"/transactions/hsbc/pending/{rid}",
                 json={"tags": ["X", "Y"]}, headers=_auth(token))
    # add: Y 已有 (dedup), z 新, x 大小寫不敏感 dedup
    r = client.patch(f"/transactions/hsbc/pending/{rid}",
                     json={"tags": ["Y", "z", "x"], "tags_mode": "add"},
                     headers=_auth(token))
    assert r.status_code == 200, r.text
    # 順序: 原 X, Y 在前, 新 z 接後 (x 跟 X 大小寫等價 → skip)
    assert r.json()["tags"] == ["X", "Y", "z"]


def test_patch_tags_mode_invalid_rejected(client, data_root):
    """tags_mode 只接受 replace / add, 其他 400."""
    token = _register(client, email="mode3@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "h"}, headers=_auth(token))
    _seed_bank_db(data_root, "hsbc",
                  pending=[{"card_no": "X", "date": "2026-06-12",
                            "desc": "x", "amount": 100}])
    rid = client.get("/transactions?bank=hsbc&kind=pending",
                     headers=_auth(token)).json()["items"][0]["raw"]["id"]
    r = client.patch(f"/transactions/hsbc/pending/{rid}",
                     json={"tags": ["X"], "tags_mode": "merge"},
                     headers=_auth(token))
    assert r.status_code == 400

    # tags_mode 沒帶 tags 也 reject
    r = client.patch(f"/transactions/hsbc/pending/{rid}",
                     json={"tags_mode": "add"},
                     headers=_auth(token))
    assert r.status_code == 400
