"""Frontend dataset cache endpoints: snapshot + incremental changes."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _register(client, email: str = "cache-user@palace.example", password: str = "SyntheticTestPassword02!") -> str:
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_cache_bank(data_root: Path, bank: str = "cathay") -> None:
    path = data_root / f"{bank}.sqlite"
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE IF NOT EXISTS accounts (
        account_no TEXT PRIMARY KEY, currency TEXT, branch TEXT, nickname TEXT, type TEXT,
        product_type TEXT, raw_balance REAL, raw_balance_date TEXT,
        excluded INTEGER NOT NULL DEFAULT 0, nickname_overwrite TEXT, updated_at TEXT NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS cards (
        card_no TEXT PRIMARY KEY, name TEXT, association TEXT, type TEXT, is_cube INTEGER,
        credit_limit REAL, used_credit REAL, statement_close_date TEXT, payment_due_date TEXT,
        active INTEGER NOT NULL DEFAULT 1, excluded INTEGER NOT NULL DEFAULT 0,
        nickname_overwrite TEXT, updated_at TEXT NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS twd_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL DEFAULT 1,
        account_no TEXT NOT NULL, txn_datetime TEXT NOT NULL, account_date TEXT,
        description TEXT, expend INTEGER, income INTEGER, balance INTEGER,
        counterparty_bank TEXT, counterparty_acct TEXT, memo TEXT,
        first_seen TEXT NOT NULL, dedup_key TEXT NOT NULL,
        category TEXT, subcategory TEXT, description_overwrite TEXT,
        auto_excluded INTEGER NOT NULL DEFAULT 0
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS card_billed_txns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL DEFAULT 1,
        card_no TEXT, bill_date TEXT, currency TEXT, consume_date TEXT, post_date TEXT,
        description TEXT, amount INTEGER, consume_country TEXT, consume_currency TEXT,
        consume_amount REAL, first_seen TEXT NOT NULL, dedup_key TEXT NOT NULL,
        category TEXT, subcategory TEXT, txn_type TEXT, description_overwrite TEXT,
        auto_excluded INTEGER NOT NULL DEFAULT 0
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS card_pending_txns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL DEFAULT 1,
        scope TEXT NOT NULL, card_no TEXT, consume_date TEXT, post_date TEXT, description TEXT,
        amount INTEGER, currency TEXT, refreshed_at TEXT NOT NULL,
        category TEXT, subcategory TEXT, txn_type TEXT, description_overwrite TEXT,
        auto_excluded INTEGER NOT NULL DEFAULT 0
    )""")
    con.execute(
        "INSERT INTO accounts (account_no,currency,nickname,type,product_type,raw_balance,raw_balance_date,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("1234567890", "TWD", "測試帳戶", "活存", "deposit", 1000, "2026-07-01", "2026-07-01T10:00:00"),
    )
    con.execute(
        "INSERT INTO cards (card_no,name,type,active,updated_at) VALUES (?,?,?,?,?)",
        ("****7015", "測試卡", "credit", 1, "2026-07-01T10:01:00"),
    )
    con.execute(
        "INSERT INTO twd_transactions (user_id,account_no,txn_datetime,account_date,description,expend,income,balance,first_seen,dedup_key,category) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (1, "1234567890", "2026-07-01", "2026-07-01", "早餐", 80, None, 920, "2026-07-01T10:02:00", "twd-1", "飲食"),
    )
    con.execute(
        "INSERT INTO card_billed_txns (user_id,card_no,currency,consume_date,post_date,description,amount,first_seen,dedup_key,category,txn_type) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (1, "****7015", "TWD", "2026-07-02", "2026-07-03", "咖啡", -120, "2026-07-01T10:03:00", "bill-1", "飲食", "spending"),
    )
    con.commit()
    con.close()


def test_cache_snapshot_returns_accounts_cards_transactions_and_cursor(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    token = _register(client)
    _seed_cache_bank(tmp_path, "cathay")

    r = client.get("/cache/snapshot", headers=_auth(token))

    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"cursor", "accounts", "cards", "transactions"}
    assert body["cursor"] >= "2026-07-01T10:03:00"
    assert body["accounts"][0]["account_no"] == "1234567890"
    assert body["cards"][0]["card_no"] == "****7015"
    assert {t["kind"] for t in body["transactions"]} == {"twd", "billed"}


def test_cache_changes_returns_only_rows_newer_than_cursor(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    token = _register(client, email="cache-user2@palace.example")
    _seed_cache_bank(tmp_path, "cathay")

    r = client.get("/cache/changes?since=2026-07-01T10:02:30", headers=_auth(token))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cursor"] >= "2026-07-01T10:03:00"
    # accounts are intentionally sent as a small full replacement set on changes,
    # because portfolio account balances don't expose a durable per-row cursor.
    assert body["accounts"][0]["account_no"] == "1234567890"
    assert body["transactions"]
    assert [t["kind"] for t in body["transactions"]] == ["billed"]


def test_cache_snapshot_same_date_order_stays_stable_after_tag_edit(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    token = _register(client, email="cache-order@palace.example")
    headers = _auth(token)
    assert client.post(
        "/accounts", json={"bank": "cathay", "label": "測試"}, headers=headers,
    ).status_code == 201
    _seed_cache_bank(tmp_path, "cathay")

    con = sqlite3.connect(str(tmp_path / "cathay.sqlite"))
    con.execute(
        "INSERT INTO card_billed_txns (user_id,card_no,currency,consume_date,post_date,description,amount,first_seen,dedup_key,category,txn_type) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (1, "****7015", "TWD", "2026-07-02", "2026-07-03", "晚餐", -360, "2026-07-01T10:04:00", "bill-2", "飲食", "spending"),
    )
    con.commit()
    con.close()

    before = client.get("/cache/snapshot", headers=headers).json()["transactions"]
    billed_before = [t for t in before if t["kind"] == "billed"]
    assert [t["description"] for t in billed_before] == ["晚餐", "咖啡"]

    target = billed_before[1]
    r = client.patch(
        f"/transactions/cathay/billed/{target['id']}",
        json={"tags": ["聚餐"]},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    after = client.get("/cache/snapshot", headers=headers).json()["transactions"]
    billed_after = [t for t in after if t["kind"] == "billed"]
    assert [(t["kind"], t["id"]) for t in billed_after] == [
        (t["kind"], t["id"]) for t in billed_before
    ]
