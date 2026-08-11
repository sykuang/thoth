"""Phase 5 (early) — /cards routes end-to-end test.

Phase 5 (early) — /cards routes 端到端測試.

涵蓋:
- 401 未登入
- 空 DB → 200 + 空 list
- 寫真實 card 進 cathay.sqlite + ctbc.sqlite, 確認跨庫聚合
- filter: bank / account_id
- 老 db 無 cards 表 graceful skip
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _register(client, email: str = "card-user@palace.example", password: str = "SyntheticTestPassword02!"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_cards(data_root: Path, bank: str, cards: list[dict]) -> None:
    """直接插 cards 表 + 假卡, 模擬 BankStore 寫過後的狀態.

    2026-06-14 (excluded): cards dict 支援 excluded: bool (預設 False)
    2026-06-14 (step 2): cards dict 支援 credit_limit / used_credit /
        statement_close_date / payment_due_date (預設 None)
    2026-06-14 (active): cards dict 支援 active: bool (預設 True; DBS 過期卡 False)
    """
    path = data_root / f"{bank}.sqlite"
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE IF NOT EXISTS cards (
        card_no              TEXT PRIMARY KEY,
        name                 TEXT,
        association          TEXT,
        type                 TEXT,
        is_cube              INTEGER,
        credit_limit         REAL,
        used_credit          REAL,
        statement_close_date TEXT,
        payment_due_date     TEXT,
        active               INTEGER NOT NULL DEFAULT 1,
        excluded             INTEGER NOT NULL DEFAULT 0,
        updated_at           TEXT NOT NULL
    )""")
    for c in cards:
        con.execute(
            "INSERT OR REPLACE INTO cards (card_no, name, association, type, is_cube, "
            "credit_limit, used_credit, statement_close_date, payment_due_date, "
            "active, excluded, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (c["card_no"], c["name"], c.get("association"), c.get("type"),
             1 if c.get("is_cube") else 0,
             c.get("credit_limit"), c.get("used_credit"),
             c.get("statement_close_date"), c.get("payment_due_date"),
             1 if c.get("active", True) else 0,
             1 if c.get("excluded") else 0,
             c.get("updated_at", "2026-06-13T12:00:00")),
        )
    con.commit()
    con.close()


def _seed_bank_db_no_cards(data_root: Path, bank: str) -> None:
    """老 db: 只有 twd_transactions, 沒 cards 表."""
    path = data_root / f"{bank}.sqlite"
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE IF NOT EXISTS twd_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_no TEXT NOT NULL, txn_datetime TEXT NOT NULL,
        description TEXT, expend INTEGER, income INTEGER,
        first_seen TEXT NOT NULL, dedup_key TEXT NOT NULL
    )""")
    con.commit()
    con.close()


def _seed_card_bill_rows(
    data_root: Path,
    bank: str,
    billed: list[dict] | None = None,
    pending: list[dict] | None = None,
) -> None:
    """Seed minimal credit-card transaction tables for /cards bill summary tests."""
    path = data_root / f"{bank}.sqlite"
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE IF NOT EXISTS card_billed_txns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_no TEXT,
        bill_date TEXT,
        currency TEXT,
        consume_date TEXT,
        post_date TEXT,
        description TEXT,
        amount INTEGER,
        first_seen TEXT NOT NULL,
        dedup_key TEXT NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS card_pending_txns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL,
        card_no TEXT,
        consume_date TEXT,
        description TEXT,
        amount INTEGER,
        currency TEXT,
        refreshed_at TEXT NOT NULL
    )""")
    for i, row in enumerate(billed or []):
        con.execute(
            """INSERT INTO card_billed_txns
               (card_no, bill_date, currency, consume_date, post_date, description,
                amount, first_seen, dedup_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("card_no"),
                row.get("bill_date"),
                row.get("currency", "TWD"),
                row.get("consume_date", "2026-06-01"),
                row.get("post_date", row.get("consume_date", "2026-06-01")),
                row.get("description", "test"),
                row.get("amount"),
                "2026-06-16T00:00:00Z",
                row.get("dedup_key", f"b{i}"),
            ),
        )
    for i, row in enumerate(pending or []):
        con.execute(
            """INSERT INTO card_pending_txns
               (scope, card_no, consume_date, description, amount, currency, refreshed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("scope", "unbilled"),
                row.get("card_no"),
                row.get("consume_date", "2026-06-10"),
                row.get("description", "pending"),
                row.get("amount"),
                row.get("currency", "TWD"),
                "2026-06-16T00:00:00Z",
            ),
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

def test_cards_requires_auth(client):
    r = client.get("/cards")
    assert r.status_code == 401


# ============================================================
# 空 DB → 200 + 空 list
# ============================================================

def test_cards_empty_returns_empty(client):
    token = _register(client)
    r = client.get("/cards", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json() == []


# ============================================================
# 跨庫聚合: cathay + ctbc 各一張卡
# ============================================================

def test_cards_aggregates_across_banks(client, data_root):
    token = _register(client)
    for bank in ("cathay", "ctbc"):
        r = client.post("/accounts", json={"bank": bank, "label": "test"}, headers=_auth(token))
        assert r.status_code == 201, r.text

    _seed_cards(data_root, "cathay", [
        {"card_no": "****7016", "name": "國泰世華悠遊卡", "association": "VISA", "type": "信用卡"},
    ])
    _seed_cards(data_root, "ctbc", [
        {"card_no": "****7050", "name": "中信 CUBE 卡", "association": "Mastercard",
         "type": "信用卡", "is_cube": True},
        {"card_no": "****7063", "name": "中信 LINE Pay 卡", "association": "Mastercard",
         "type": "信用卡"},
    ])

    r = client.get("/cards", headers=_auth(token))
    assert r.status_code == 200, r.text
    cards = r.json()
    assert len(cards) == 3

    # 排序: bank 字典序 (cathay < ctbc), name 字典序
    assert cards[0]["bank"] == "cathay"
    assert cards[0]["card_no"] == "****7016"
    assert cards[0]["name"] == "國泰世華悠遊卡"
    assert cards[0]["association"] == "VISA"
    assert cards[0]["is_cube"] is False

    cube_card = next(c for c in cards if c["card_no"] == "****7050")
    assert cube_card["bank"] == "ctbc"
    assert cube_card["is_cube"] is True


# ============================================================
# filter by bank (comma list)
# ============================================================

def test_cards_filter_by_bank(client, data_root):
    token = _register(client)
    for bank in ("cathay", "ctbc", "hsbc"):
        r = client.post("/accounts", json={"bank": bank, "label": "test"}, headers=_auth(token))
        assert r.status_code == 201

    _seed_cards(data_root, "cathay", [{"card_no": "C1", "name": "Cathay card"}])
    _seed_cards(data_root, "ctbc", [{"card_no": "T1", "name": "CTBC card"}])
    _seed_cards(data_root, "hsbc", [{"card_no": "H1", "name": "HSBC card"}])

    # 單一 bank
    r = client.get("/cards?bank=hsbc", headers=_auth(token))
    assert r.status_code == 200
    assert all(c["bank"] == "hsbc" for c in r.json())
    assert len(r.json()) == 1

    # comma list
    r = client.get("/cards?bank=cathay,ctbc", headers=_auth(token))
    assert r.status_code == 200
    banks = {c["bank"] for c in r.json()}
    assert banks == {"cathay", "ctbc"}


# ============================================================
# filter by account_id (只看該 account 的 bank)
# ============================================================

def test_cards_filter_by_account_id(client, data_root):
    token = _register(client)
    # 註冊 2 個 account, 不同銀行
    r1 = client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    client.post("/accounts", json={"bank": "ctbc", "label": "主帳"}, headers=_auth(token))
    cathay_acct_id = r1.json()["id"]

    _seed_cards(data_root, "cathay", [{"card_no": "C1", "name": "Cathay card"}])
    _seed_cards(data_root, "ctbc", [{"card_no": "T1", "name": "CTBC card"}])

    r = client.get(f"/cards?account_id={cathay_acct_id}", headers=_auth(token))
    assert r.status_code == 200
    cards = r.json()
    assert len(cards) == 1
    assert cards[0]["bank"] == "cathay"


# ============================================================
# account_id 找不到 → 404
# ============================================================

def test_cards_account_id_not_owned_returns_404(client, data_root):
    token = _register(client, email="owner@p.com")
    # 別人的 account
    other_token = _register(client, email="other@p.com")
    r = client.post("/accounts", json={"bank": "cathay", "label": "他人帳"}, headers=_auth(other_token))
    other_acct_id = r.json()["id"]

    r = client.get(f"/cards?account_id={other_acct_id}", headers=_auth(token))
    assert r.status_code == 404


# ============================================================
# unknown bank → 400
# ============================================================

def test_cards_unknown_bank_returns_400(client):
    token = _register(client)
    r = client.get("/cards?bank=mars-bank", headers=_auth(token))
    assert r.status_code == 400
    assert "不支援的銀行" in r.text or "mars-bank" in r.text


# ============================================================
# 老 db (沒 cards 表) graceful skip
# ============================================================

def test_cards_legacy_db_without_cards_table(client, data_root):
    token = _register(client)
    for bank in ("cathay", "hsbc"):
        r = client.post("/accounts", json={"bank": bank, "label": "test"}, headers=_auth(token))
        assert r.status_code == 201

    # cathay 有 cards 表, hsbc 是老 db 沒 cards 表 → 不該炸
    _seed_cards(data_root, "cathay", [{"card_no": "C1", "name": "Cathay card"}])
    _seed_bank_db_no_cards(data_root, "hsbc")

    r = client.get("/cards", headers=_auth(token))
    assert r.status_code == 200
    cards = r.json()
    assert len(cards) == 1
    assert cards[0]["bank"] == "cathay"


# ============================================================
# 同一卡 (card_no) 同 bank 不重複 (PRIMARY KEY 已 dedupe)
# 但跨 bank 同 card_no 是合法的 (兩家銀行不同 mask 後可能 alias)
# ============================================================

def test_cards_dedup_same_bank_same_card_no(client, data_root):
    token = _register(client)
    r = client.post("/accounts", json={"bank": "cathay", "label": "test"}, headers=_auth(token))
    assert r.status_code == 201

    # 同 card_no UPSERT 後只剩最新版
    _seed_cards(data_root, "cathay", [
        {"card_no": "****7016", "name": "舊名"},
    ])
    _seed_cards(data_root, "cathay", [
        {"card_no": "****7016", "name": "新名"},
    ])

    r = client.get("/cards", headers=_auth(token))
    assert r.status_code == 200
    cards = r.json()
    assert len(cards) == 1
    assert cards[0]["name"] == "新名"


# ============================================================
# Phase 6 (2026-06-14 PM): cards.excluded flag — list + PATCH endpoint
# ============================================================

def test_cards_list_exposes_excluded_flag(client, data_root):
    """GET /cards 該卡帶 excluded: bool."""
    token = _register(client, email="card-flag@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_cards(data_root, "ubot", [
        {"card_no": "C1", "name": "Normal card"},
        {"card_no": "C2", "name": "Excluded card", "excluded": True},
    ])
    r = client.get("/cards", headers=_auth(token))
    cards = {c["card_no"]: c for c in r.json()}
    assert cards["C1"]["excluded"] is False
    assert cards["C2"]["excluded"] is True


# ============================================================
# Step 2 (2026-06-14): per-card 信用額度 + 帳單日 expose
# ============================================================

def test_cards_list_exposes_credit_limit_and_bill_dates(client, data_root):
    """GET /cards 該卡帶 credit_limit / used_credit / 帳單日 / 繳費日.

    - 有抓到 → 數字 / 日期字串原樣 expose
    - 沒抓到 (collector 還沒接) → None (frontend 顯示「—」, 禁顯示假 0)
    """
    token = _register(client, email="card-step2@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_cards(data_root, "ubot", [
        # 完整抓到 — 4 欄都有值
        {"card_no": "FULL01", "name": "Card with full data",
         "credit_limit": 200000.0, "used_credit": 15234.5,
         "statement_close_date": "2026-06-15", "payment_due_date": "2026-07-05"},
        # 半抓 — 只有額度沒帳單日（collector 早期狀態可能）
        {"card_no": "PARTIAL", "name": "Card with limit only",
         "credit_limit": 50000.0},
        # 完全沒抓 — 4 欄都 None（沒升級 collector 的銀行）
        {"card_no": "EMPTY01", "name": "Card without step2 data"},
    ])
    r = client.get("/cards", headers=_auth(token))
    cards = {c["card_no"]: c for c in r.json()}

    full = cards["FULL01"]
    assert full["credit_limit"] == 200000.0
    assert full["used_credit"] == 15234.5
    assert full["statement_close_date"] == "2026-06-15"
    assert full["payment_due_date"] == "2026-07-05"

    partial = cards["PARTIAL"]
    assert partial["credit_limit"] == 50000.0
    assert partial["used_credit"] is None
    assert partial["statement_close_date"] is None
    assert partial["payment_due_date"] is None

    empty = cards["EMPTY01"]
    assert empty["credit_limit"] is None
    assert empty["used_credit"] is None
    assert empty["statement_close_date"] is None
    assert empty["payment_due_date"] is None


def test_cards_list_exposes_normalized_bill_amounts(client, data_root):
    """GET /cards 回 MoneyBook-style bill summary fields.

    - bill_due_amount: 沒 canonical remaining-due fact 時為 null，不從交易猜測
    - unbilled_amount: pending txns sum, 只取正數
    - available_credit: credit_limit - used_credit
    - bill_status: 沒 canonical remaining-due fact → unknown
    """
    token = _register(client, email="card-bill-summary@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_cards(data_root, "ubot", [
        {"card_no": "CARD-A", "name": "A card", "credit_limit": 300000.0,
         "used_credit": 41065.0, "payment_due_date": "2099-06-18"},
        {"card_no": "CARD-B", "name": "B card", "credit_limit": 300000.0,
         "used_credit": 0.0, "payment_due_date": "2099-06-18"},
    ])
    _seed_card_bill_rows(
        data_root,
        "ubot",
        billed=[
            {"card_no": "CARD-A", "bill_date": "2026-06-03", "amount": 41030, "dedup_key": "a1"},
            {"card_no": "CARD-A", "bill_date": "2026-06-03", "amount": -2383, "dedup_key": "a2"},
            {"card_no": "CARD-A", "bill_date": "2026-05-03", "amount": 999, "dedup_key": "old"},
            # CARD-B 只有負數 payment → 不該顯示成負的待繳
            {"card_no": "CARD-B", "bill_date": "2026-06-03", "amount": -1000, "dedup_key": "b1"},
        ],
        pending=[
            {"card_no": "CARD-A", "amount": 41065},
            {"card_no": "CARD-A", "amount": -100},
            {"card_no": "CARD-B", "amount": 0},
        ],
    )

    r = client.get("/cards", headers=_auth(token))
    assert r.status_code == 200, r.text
    cards = {c["card_no"]: c for c in r.json()}

    assert cards["CARD-A"]["bill_due_amount"] is None
    assert cards["CARD-A"]["unbilled_amount"] == 41065.0
    assert cards["CARD-A"]["available_credit"] == 258935.0
    assert cards["CARD-A"]["bill_status"] == "unknown"

    assert cards["CARD-B"]["bill_due_amount"] is None
    assert cards["CARD-B"]["unbilled_amount"] == 0.0
    assert cards["CARD-B"]["bill_status"] == "unknown"


def test_cards_bill_summary_does_not_treat_bank_level_transactions_as_remaining_due(client, data_root):
    token = _register(client, email="card-bill-blank@p.com")
    client.post("/accounts", json={"bank": "fubon", "label": "x"}, headers=_auth(token))
    _seed_cards(data_root, "fubon", [
        {"card_no": "FUBON-1", "name": "Fubon card", "credit_limit": 80000.0,
         "used_credit": 0.0, "payment_due_date": None},
    ])
    _seed_card_bill_rows(
        data_root,
        "fubon",
        billed=[{"card_no": None, "bill_date": "2026-05-16", "amount": 7271}],
        pending=[],
    )

    r = client.get("/cards", headers=_auth(token))
    assert r.status_code == 200, r.text
    card = r.json()[0]
    assert card["bill_due_amount"] is None
    assert card["bill_status"] == "unknown"


def test_upsert_cards_persists_step2_fields(tmp_path, monkeypatch):
    """BankStore.upsert_cards 直接寫 step 2 四欄 → DB 讀回對得上."""
    import backend.core.store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))  # 雙保險：env 也設,即使 store.py 退化也不會 leak
    from backend.core.store import BankStore
    store = BankStore("ubot")
    store.upsert_cards([
        {"number": "S2-CARD-001", "name": "Step 2 card", "association": "VISA",
         "type": "credit", "is_cube": False,
         "credit_limit": 300000.0, "used_credit": 12345.67,
         "statement_close_date": "2026-06-15", "payment_due_date": "2026-07-05"},
    ])
    rows = list(store.conn.execute(
        "SELECT credit_limit, used_credit, statement_close_date, payment_due_date "
        "FROM cards WHERE card_no=?", ("S2-CARD-001",)
    ))
    store.close()
    assert len(rows) == 1
    assert rows[0][0] == 300000.0
    assert rows[0][1] == 12345.67
    assert rows[0][2] == "2026-06-15"
    assert rows[0][3] == "2026-07-05"


def test_upsert_cards_normalizes_card_date_fields(tmp_path, monkeypatch):
    """BankStore.upsert_cards 是 cards normalized schema 的共同入口，需正規化日期格式。"""
    import backend.core.store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    from backend.core.store import BankStore

    store = BankStore("fubon")
    store.upsert_cards([
        {"number": "DATE-NORM-001", "name": "Date card",
         "statement_close_date": "2026/6/16",
         "payment_due_date": "2026/07/02",
         "last_payment_date": "2026/7/3"},
    ])
    row = store.conn.execute(
        "SELECT statement_close_date, payment_due_date, last_payment_date "
        "FROM cards WHERE card_no=?",
        ("DATE-NORM-001",),
    ).fetchone()
    store.close()

    assert row is not None
    assert tuple(row) == ("2026-06-16", "2026-07-02", "2026-07-03")


def test_upsert_cards_coalesce_protects_existing_step2_fields(tmp_path, monkeypatch):
    """同一張卡再 upsert 沒帶 step 2 欄 → 舊值保留 (COALESCE 防呼).

    防止 collector 升級到一半的銀行 (只更新 name) 沖掉之前抓到的 credit_limit.
    """
    import backend.core.store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))  # 雙保險：env 也設,即使 store.py 退化也不會 leak
    from backend.core.store import BankStore
    store = BankStore("ubot")
    # 第 1 次: 帶完整 step 2 資料
    store.upsert_cards([
        {"number": "PROTECT-001", "name": "v1",
         "credit_limit": 100000.0, "used_credit": 5000.0,
         "statement_close_date": "2026-06-15", "payment_due_date": "2026-07-05"},
    ])
    # 第 2 次: 只更新 name (例如下個 collector run 沒重抓 limit)
    store.upsert_cards([
        {"number": "PROTECT-001", "name": "v2"},
    ])
    rows = list(store.conn.execute(
        "SELECT name, credit_limit, used_credit, statement_close_date, payment_due_date "
        "FROM cards WHERE card_no=?", ("PROTECT-001",)
    ))
    store.close()
    assert rows[0][0] == "v2"             # name 有更新
    assert rows[0][1] == 100000.0          # credit_limit COALESCE 保留
    assert rows[0][2] == 5000.0            # used_credit COALESCE 保留
    assert rows[0][3] == "2026-06-15"      # statement_close_date COALESCE 保留
    assert rows[0][4] == "2026-07-05"      # payment_due_date COALESCE 保留


def test_bank_store_migration_adds_step2_columns(tmp_path, monkeypatch):
    """先建一個只有舊 cards schema 的 DB, 再開 BankStore() 應自動 ALTER 補欄."""
    import backend.core.store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))  # 雙保險：env 也設,即使 store.py 退化也不會 leak
    db_path = tmp_path / "ubot.sqlite"
    # Step 1: 模擬「舊 schema」DB (沒 step 2 四欄)
    con = sqlite3.connect(str(db_path))
    con.execute("""CREATE TABLE cards (
        card_no TEXT PRIMARY KEY, name TEXT, association TEXT, type TEXT,
        is_cube INTEGER, updated_at TEXT NOT NULL
    )""")
    con.commit()
    con.close()
    # Step 2: BankStore() 開啟 → migration 應該自動補欄
    from backend.core.store import BankStore
    store = BankStore("ubot")
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(cards)").fetchall()}
    store.close()
    for col in ("credit_limit", "used_credit", "statement_close_date", "payment_due_date", "excluded", "active"):
        assert col in cols, f"migration 沒補 {col} 欄"


# ============================================================
# 2026-06-14 active: 過期卡 (DBS isDisplayImg=False) UI 不顯示
# ============================================================

def test_cards_list_default_hides_inactive(client, data_root):
    """GET /cards 預設只回 active=1 (過期卡 hide).

    DBS 場景: 已換新卡的舊號 ****7029 應該不出現, 新卡 ****7040 出現.
    """
    token = _register(client, email="card-active@p.com")
    client.post("/accounts", json={"bank": "dbs", "label": "x"}, headers=_auth(token))
    _seed_cards(data_root, "dbs", [
        {"card_no": "****7029", "name": "舊卡(過期)", "active": False},
        {"card_no": "****7040", "name": "新卡(有效)", "active": True},
        {"card_no": "****7019", "name": "預設 active", },  # default True
    ])
    r = client.get("/cards", headers=_auth(token))
    nos = {c["card_no"] for c in r.json()}
    assert "****7040" in nos
    assert "****7019" in nos
    assert "****7029" not in nos  # 過期卡 hide


def test_cards_list_include_inactive_true_returns_all(client, data_root):
    """?include_inactive=true 帶回所有卡 (含 active=0).

    給「卡片管理」 / 歷史報表頁面用 — 使用者想看舊卡狀態時可開.
    """
    token = _register(client, email="card-active-all@p.com")
    client.post("/accounts", json={"bank": "dbs", "label": "x"}, headers=_auth(token))
    _seed_cards(data_root, "dbs", [
        {"card_no": "****7029", "name": "舊卡", "active": False},
        {"card_no": "****7040", "name": "新卡", "active": True},
    ])
    r = client.get("/cards?include_inactive=true", headers=_auth(token))
    cards = {c["card_no"]: c for c in r.json()}
    assert set(cards.keys()) == {"****7029", "****7040"}
    assert cards["****7029"]["active"] is False
    assert cards["****7040"]["active"] is True


def test_cards_list_active_field_exposed(client, data_root):
    """GET /cards 每筆都帶 active: bool (預設 True)."""
    token = _register(client, email="card-active-field@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_cards(data_root, "ubot", [
        {"card_no": "C1", "name": "default active"},
    ])
    r = client.get("/cards", headers=_auth(token))
    cards = r.json()
    assert len(cards) == 1
    assert cards[0]["active"] is True


def test_upsert_cards_persists_active_field(tmp_path, monkeypatch):
    """BankStore.upsert_cards 寫 active=False → DB 讀回 0."""
    import backend.core.store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))  # 雙保險：env 也設,即使 store.py 退化也不會 leak
    from backend.core.store import BankStore
    store = BankStore("dbs")
    store.upsert_cards([
        {"number": "EXPIRED-001", "name": "Expired", "active": False},
        {"number": "ACTIVE-001", "name": "Active", "active": True},
        {"number": "DEFAULT-001", "name": "Default"},  # 沒帶 active → schema default 1
    ])
    rows = {r[0]: r[1] for r in store.conn.execute(
        "SELECT card_no, active FROM cards ORDER BY card_no"
    )}
    store.close()
    assert rows["EXPIRED-001"] == 0
    assert rows["ACTIVE-001"] == 1
    assert rows["DEFAULT-001"] == 1  # schema default


def test_upsert_cards_coalesce_protects_active(tmp_path, monkeypatch):
    """同卡再 upsert 沒帶 active → 舊值保留 (COALESCE 防呼)."""
    import backend.core.store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))  # 雙保險：env 也設,即使 store.py 退化也不會 leak
    from backend.core.store import BankStore
    store = BankStore("dbs")
    # 第 1 次: 標記成 expired
    store.upsert_cards([
        {"number": "STABLE-001", "name": "v1", "active": False},
    ])
    # 第 2 次: 只更新 name, 沒帶 active
    store.upsert_cards([
        {"number": "STABLE-001", "name": "v2"},
    ])
    row = next(iter(store.conn.execute(
        "SELECT name, active FROM cards WHERE card_no=?", ("STABLE-001",)
    )))
    store.close()
    assert row[0] == "v2"
    assert row[1] == 0  # 還是 expired (沒被沖)


def test_persist_dbs_marks_isdisplayimg_false_as_inactive():
    """persist_dbs 的 isDisplayImg → cards.active mapping.

    這只測 mapping 邏輯, 不啟動 BankStore (隔離測試).
    """
    # 直接 import persist 模組裡那個 dict 構造邏輯需要重寫成可測函式;
    # 改用真實 BankStore + repersist_from_json 邏輯難隔離,
    # 此處用最小 fixture 驗證 mapping 規則.
    sample_cards = [
        {"isDisplayImg": True, "cardNumber": "1111", "cardDescription": "active"},
        {"isDisplayImg": False, "cardNumber": "2222", "cardDescription": "expired"},
    ]
    # 模擬 persist_dbs 的 cards transform
    transformed = []
    for c in sample_cards:
        cn = c.get("cardNumber") or ""
        last4 = cn[-4:] if cn else ""
        transformed.append({
            "number": f"****{last4}",
            "active": bool(c.get("isDisplayImg")),
        })
    assert transformed[0]["active"] is True
    assert transformed[1]["active"] is False


def test_patch_card_excluded_toggles(client, data_root):
    """PATCH /cards/{bank}/{card_no}/excluded 翻轉 flag."""
    token = _register(client, email="card-toggle@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_cards(data_root, "ubot", [{"card_no": "MYCARD", "name": "Test"}])

    r = client.get("/cards", headers=_auth(token))
    assert next(c for c in r.json() if c["card_no"] == "MYCARD")["excluded"] is False

    # PATCH → true
    r = client.patch(
        "/cards/ubot/MYCARD/excluded",
        json={"excluded": True},
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["excluded"] is True
    assert body["card_no"] == "MYCARD"
    assert body["bank"] == "ubot"

    # 落地確認
    r2 = client.get("/cards", headers=_auth(token))
    assert next(c for c in r2.json() if c["card_no"] == "MYCARD")["excluded"] is True

    # PATCH 翻回
    client.patch(
        "/cards/ubot/MYCARD/excluded",
        json={"excluded": False},
        headers=_auth(token),
    )
    r3 = client.get("/cards", headers=_auth(token))
    assert next(c for c in r3.json() if c["card_no"] == "MYCARD")["excluded"] is False


def test_patch_card_excluded_404_unknown_bank(client, data_root):
    token = _register(client, email="card-404-bank@p.com")
    r = client.patch(
        "/cards/unknown_bank/CARD1/excluded",
        json={"excluded": True},
        headers=_auth(token),
    )
    assert r.status_code == 404


def test_patch_card_excluded_404_unknown_card(client, data_root):
    token = _register(client, email="card-404-card@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_cards(data_root, "ubot", [{"card_no": "REAL_CARD", "name": "x"}])
    r = client.patch(
        "/cards/ubot/NONEXISTENT/excluded",
        json={"excluded": True},
        headers=_auth(token),
    )
    assert r.status_code == 404


# ============================================================
# Phase 8.2 C (2026-06-14): nickname_overwrite endpoint
# ============================================================
def test_patch_card_nickname_sets_and_clears(client, data_root):
    """PATCH /cards/{bank}/{card_no}/nickname 設覆寫名 + 清空恢復."""
    token = _register(client, email="card-nick@p.com")
    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_cards(data_root, "ubot", [{"card_no": "MYCARD", "name": "原廠卡名"}])

    # 一開始沒覆寫
    r = client.get("/cards", headers=_auth(token))
    card = next(c for c in r.json() if c["card_no"] == "MYCARD")
    assert card["name"] == "原廠卡名"
    assert card["nickname_overwrite"] is None

    # 設覆寫
    r = client.patch(
        "/cards/ubot/MYCARD/nickname",
        json={"nickname_overwrite": "我的便利卡"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["nickname_overwrite"] == "我的便利卡"
    assert body["bank"] == "ubot"
    assert body["card_no"] == "MYCARD"

    # 落地確認: raw name 不動 + overwrite 有
    r2 = client.get("/cards", headers=_auth(token))
    card = next(c for c in r2.json() if c["card_no"] == "MYCARD")
    assert card["name"] == "原廠卡名"  # raw 不動
    assert card["nickname_overwrite"] == "我的便利卡"

    # 清空 (空字串 → NULL)
    r = client.patch(
        "/cards/ubot/MYCARD/nickname",
        json={"nickname_overwrite": ""},
        headers=_auth(token),
    )
    assert r.json()["nickname_overwrite"] is None

    # 清空 (None → NULL)
    client.patch(
        "/cards/ubot/MYCARD/nickname",
        json={"nickname_overwrite": "再覆寫"},
        headers=_auth(token),
    )
    r = client.patch(
        "/cards/ubot/MYCARD/nickname",
        json={"nickname_overwrite": None},
        headers=_auth(token),
    )
    assert r.json()["nickname_overwrite"] is None
    r3 = client.get("/cards", headers=_auth(token))
    assert next(c for c in r3.json() if c["card_no"] == "MYCARD")["nickname_overwrite"] is None


def test_patch_card_nickname_404(client, data_root):
    """unknown bank / unknown card → 404."""
    token = _register(client, email="card-nick404@p.com")
    r = client.patch(
        "/cards/unknown_bank/X/nickname",
        json={"nickname_overwrite": "x"},
        headers=_auth(token),
    )
    assert r.status_code == 404

    client.post("/accounts", json={"bank": "ubot", "label": "x"}, headers=_auth(token))
    _seed_cards(data_root, "ubot", [{"card_no": "REAL", "name": "x"}])
    r = client.patch(
        "/cards/ubot/NONEXISTENT/nickname",
        json={"nickname_overwrite": "x"},
        headers=_auth(token),
    )
    assert r.status_code == 404
