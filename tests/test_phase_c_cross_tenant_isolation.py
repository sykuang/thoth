"""Phase C (2026-06-17) — Multi-user row-level isolation 攻擊面 enforcement test.

把每個 user-facing endpoint 都丟一條 cross-tenant attack:
  user A 註冊 → seed user A's bank data
  user B 註冊 → 拿 user B token 去打 user A 的 resource id
  預期: 404 / 空 list (絕不洩漏 row)

涵蓋: GET /transactions/{bank}/{kind}/{txn_id}, PATCH 同 path, GET /cards/{bank}/{card_no},
PATCH /cards/{bank}/{card_no}/nickname|excluded, PATCH /portfolio/accounts/{bank}/{account_no}/nickname|excluded,
GET /transactions/, GET /cards/, GET /portfolio/summary 不應該回 user A 的 row。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


def _register(client, email: str, password: str = "SyntheticTestPassword02!") -> str:
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def data_root():
    return Path(os.environ["BANK_DATA_ROOT"])


def _seed_alice_data(data_root: Path) -> tuple[int, str, str]:
    """Seed cathay.sqlite with Alice's (user_id=1) data: 1 txn, 1 card, 1 account.

    回 (twd_txn_id, card_no, account_no) 給 attack 端用。
    """
    path = data_root / "cathay.sqlite"
    con = sqlite3.connect(str(path))
    # Schema 故意不帶 user_id — open_bank_conn 第一次摸會 lazy ALTER 補
    con.execute("""CREATE TABLE IF NOT EXISTS twd_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_no TEXT NOT NULL, txn_datetime TEXT NOT NULL,
        description TEXT, expend INTEGER, income INTEGER, balance INTEGER,
        first_seen TEXT NOT NULL, dedup_key TEXT NOT NULL,
        flow_type TEXT NOT NULL DEFAULT 'expense',
        is_subscription INTEGER NOT NULL DEFAULT 0,
        category TEXT, subcategory TEXT,
        auto_excluded INTEGER NOT NULL DEFAULT 0
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS cards (
        card_no TEXT PRIMARY KEY, name TEXT, association TEXT, type TEXT,
        is_cube INTEGER, credit_limit REAL, used_credit REAL,
        statement_close_date TEXT, payment_due_date TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        excluded INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS accounts (
        account_no TEXT PRIMARY KEY, currency TEXT, branch TEXT,
        nickname TEXT, type TEXT, product_type TEXT,
        raw_balance REAL, raw_balance_date TEXT,
        excluded INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )""")
    cur = con.execute(
        "INSERT INTO twd_transactions (account_no, txn_datetime, description, expend, "
        "first_seen, dedup_key) VALUES (?, ?, ?, ?, ?, ?)",
        ("ALICE-ACCT-001", "2026-06-15T12:00:00", "Alice secret coffee",
         150, "2026-06-15T12:00:00", "alice-dedup-1"),
    )
    twd_id = cur.lastrowid
    assert twd_id is not None
    con.execute(
        "INSERT INTO cards (card_no, name, updated_at) VALUES (?, ?, ?)",
        ("ALICE-CARD-9999", "Alice's Card", "2026-06-15T12:00:00"),
    )
    con.execute(
        "INSERT INTO accounts (account_no, currency, type, updated_at) VALUES (?, ?, ?, ?)",
        ("ALICE-ACCT-001", "TWD", "savings", "2026-06-15T12:00:00"),
    )
    con.commit()
    con.close()
    # 注意: 此時還沒有 user_id 欄, 由 endpoint 第一次摸時 lazy ALTER (legacy
    # backfill user_id=1, 對齊 BankStore._migrate)。
    return (twd_id, "ALICE-CARD-9999", "ALICE-ACCT-001")


# ============================================================
# Cross-tenant attack tests
# ============================================================

def test_user_b_cannot_read_user_a_txn_detail(client, data_root):
    """user B 拿 user A 的 txn_id 讀 detail → 404 (即使 id 存在於 DB)."""
    # Alice 先註冊 (拿到 user_id=1) + seed 資料 (legacy backfill 都會 user_id=1)
    _register(client, "alice@palace.example")
    twd_id, _, _ = _seed_alice_data(data_root)

    # Bob 註冊 (拿到 user_id=2)
    bob_token = _register(client, "bob@palace.example")

    # Bob 試讀 Alice 的 txn
    r = client.get(f"/transactions/cathay/twd/{twd_id}", headers=_auth(bob_token))
    # 預期 404 (找不到此筆交易) — 不能洩漏 row
    # 注意: 也可能 403/404 由 _assert_bank_ownership 擋, Alice 才有 cathay account, Bob 沒
    assert r.status_code in (403, 404), f"Bob 不該讀到 Alice 的 txn! got {r.status_code}: {r.text}"


def test_user_b_cannot_update_user_a_txn(client, data_root):
    """user B 拿 user A 的 txn_id PATCH → 404."""
    _register(client, "alice2@palace.example")
    twd_id, _, _ = _seed_alice_data(data_root)
    bob_token = _register(client, "bob2@palace.example")

    r = client.patch(
        f"/transactions/cathay/twd/{twd_id}",
        json={"category": "hacked_by_bob"},
        headers=_auth(bob_token),
    )
    assert r.status_code in (403, 404), f"Bob 不該改 Alice 的 txn! got {r.status_code}"

    # 驗證 Alice 的 row 沒被改 — 直接讀 db
    con = sqlite3.connect(str(data_root / "cathay.sqlite"))
    row = con.execute(
        "SELECT category FROM twd_transactions WHERE id = ?", (twd_id,),
    ).fetchone()
    con.close()
    # 沒被改成 hacked_by_bob (應該還是 None / 原值)
    assert row[0] != "hacked_by_bob", "Alice 的 row 被 Bob PATCH 攻擊改寫!"


def test_user_b_transactions_list_does_not_leak_user_a_rows(client, data_root):
    """user B GET /transactions/ 不該回 user A 的 row."""
    _register(client, "alice3@palace.example")
    _seed_alice_data(data_root)
    bob_token = _register(client, "bob3@palace.example")

    r = client.get("/transactions/", headers=_auth(bob_token))
    assert r.status_code == 200
    items = r.json().get("items", [])
    # Bob 沒 seed 任何 cathay account → list_for_user 應該回空 → items=[]
    leaked = [i for i in items if "alice" in (i.get("description") or "").lower()]
    assert not leaked, f"Bob 的 /transactions/ 洩漏了 Alice 的 row: {leaked}"


def test_user_b_cards_list_does_not_leak_user_a_cards(client, data_root):
    """user B GET /cards/ 不該回 user A 的 card."""
    _register(client, "alice4@palace.example")
    _, card_no, _ = _seed_alice_data(data_root)
    bob_token = _register(client, "bob4@palace.example")

    r = client.get("/cards/", headers=_auth(bob_token))
    assert r.status_code == 200
    leaked = [c for c in r.json() if c.get("card_no") == card_no]
    assert not leaked, f"Bob 的 /cards/ 洩漏了 Alice 的卡: {leaked}"


def test_user_b_cannot_patch_user_a_card_nickname(client, data_root):
    """user B PATCH /cards/{bank}/{card_no}/nickname → 404."""
    _register(client, "alice5@palace.example")
    _, card_no, _ = _seed_alice_data(data_root)
    bob_token = _register(client, "bob5@palace.example")

    r = client.patch(
        f"/cards/cathay/{card_no}/nickname",
        json={"nickname_overwrite": "Bob_hacked"},
        headers=_auth(bob_token),
    )
    assert r.status_code in (403, 404), f"Bob 不該改 Alice 的卡! got {r.status_code}"

    # 驗證 Alice 卡沒被改
    con = sqlite3.connect(str(data_root / "cathay.sqlite"))
    row = con.execute(
        "SELECT nickname_overwrite FROM cards WHERE card_no = ?", (card_no,),
    ).fetchone()
    con.close()
    # nickname_overwrite 欄可能不存在 (老 schema), 若存在不該是 Bob_hacked
    if row is not None:
        assert (row[0] != "Bob_hacked"), "Alice 的卡被 Bob 改寫!"


def test_user_b_cannot_patch_user_a_card_excluded(client, data_root):
    _register(client, "alice6@palace.example")
    _, card_no, _ = _seed_alice_data(data_root)
    bob_token = _register(client, "bob6@palace.example")

    r = client.patch(
        f"/cards/cathay/{card_no}/excluded",
        json={"excluded": True},
        headers=_auth(bob_token),
    )
    assert r.status_code in (403, 404), f"Bob 不該改 Alice 的 excluded! got {r.status_code}"


def test_user_b_cannot_patch_user_a_account_nickname(client, data_root):
    _register(client, "alice7@palace.example")
    _, _, acct_no = _seed_alice_data(data_root)
    bob_token = _register(client, "bob7@palace.example")

    r = client.patch(
        f"/portfolio/accounts/cathay/{acct_no}/nickname",
        json={"nickname_overwrite": "Bob_hacked_acct"},
        headers=_auth(bob_token),
    )
    assert r.status_code in (403, 404), f"Bob 不該改 Alice 的 account nickname! got {r.status_code}"


def test_user_b_cannot_patch_user_a_account_excluded(client, data_root):
    _register(client, "alice8@palace.example")
    _, _, acct_no = _seed_alice_data(data_root)
    bob_token = _register(client, "bob8@palace.example")

    r = client.patch(
        f"/portfolio/accounts/cathay/{acct_no}/excluded",
        json={"excluded": True},
        headers=_auth(bob_token),
    )
    assert r.status_code in (403, 404), f"Bob 不該改 Alice 的 account excluded! got {r.status_code}"


def test_user_b_portfolio_summary_does_not_count_user_a_assets(client, data_root):
    """user B GET /portfolio/summary 的 total_assets/by_bank 不該含 user A 的數字."""
    _register(client, "alice9@palace.example")
    _seed_alice_data(data_root)
    bob_token = _register(client, "bob9@palace.example")

    r = client.get("/portfolio/summary", headers=_auth(bob_token))
    assert r.status_code == 200
    body = r.json()
    # Bob 沒任何資料 → total_assets = 0, by_bank 不該有 cathay
    assert body.get("total_assets", -1) == 0, f"Bob 的 summary 含 Alice 的資產! {body}"
    cathay_in_bob = [b for b in body.get("by_bank", []) if b.get("bank") == "cathay" and b.get("assets")]
    assert not cathay_in_bob, f"Bob 的 by_bank 含 Alice 的 cathay row: {cathay_in_bob}"
