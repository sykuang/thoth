"""Phase C-Suggestion (2026-06-17): LocalFernetBackend defense-in-depth.

驗 *_acct API 帶 expected_owner_user_id 時 verify account 屬不屬 user:
- 不傳 → backward compat, 不 check
- 傳對的 → 正常 work
- 傳錯的 → raise PermissionError (defense-in-depth — 即使 router IDOR 漏洞繞 owner check 也擋住)
"""
from __future__ import annotations

import importlib

import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def store_with_two_accounts(tmp_path, monkeypatch):
    """user A (id=1) 有 cathay account_id=1, user B (id=2) 有 cathay account_id=2,
    都已寫 password cred. 回 (LocalFernetBackend, acct_a_id, acct_b_id)。
    """
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret-32+bytes-padding-padding-padding!")
    monkeypatch.setenv("SERVER_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("REGISTER_DELAY_SECONDS", "0")

    # reload db / creds_store / users so fresh env applies
    import backend.server.db as db_mod
    import backend.server.creds_store as cs_mod
    import backend.server.users as users_mod
    importlib.reload(db_mod)
    importlib.reload(cs_mod)
    importlib.reload(users_mod)

    # 直接寫 bank_accounts + bank_credentials_v2 — 不走 router
    with db_mod.get_conn() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, created_at) VALUES (1, 'a@x', 'hash', '2026-01-01')",
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, created_at) VALUES (2, 'b@x', 'hash', '2026-01-01')",
        )
        conn.execute(
            "INSERT INTO bank_accounts (id, user_id, bank, label, created_at, updated_at) "
            "VALUES (1, 1, 'cathay', '我的國泰', '2026-01-01', '2026-01-01')",
        )
        conn.execute(
            "INSERT INTO bank_accounts (id, user_id, bank, label, created_at, updated_at) "
            "VALUES (2, 2, 'cathay', '別人國泰', '2026-01-01', '2026-01-01')",
        )

    store = cs_mod.LocalFernetBackend()
    store.put_acct(1, "password", "user-A-secret")
    store.put_acct(2, "password", "user-B-secret")
    return store, 1, 2


def test_get_acct_without_owner_check_backward_compat(store_with_two_accounts):
    """不傳 expected_owner_user_id → 原行為, 不 check, 任 caller 可拿."""
    store, acct_a, acct_b = store_with_two_accounts
    assert store.get_acct(acct_a, "password") == "user-A-secret"
    assert store.get_acct(acct_b, "password") == "user-B-secret"


def test_get_acct_correct_owner_passes(store_with_two_accounts):
    """user 1 拿 acct 1 (owner 對) → 正常 decrypt."""
    store, acct_a, _ = store_with_two_accounts
    assert store.get_acct(acct_a, "password", expected_owner_user_id=1) == "user-A-secret"


def test_get_acct_wrong_owner_raises(store_with_two_accounts):
    """user 1 拿 acct 2 (owner 是 user 2) → PermissionError (即使 router 漏 check 也擋)."""
    store, _, acct_b = store_with_two_accounts
    with pytest.raises(PermissionError, match="owner mismatch"):
        store.get_acct(acct_b, "password", expected_owner_user_id=1)


def test_get_all_for_account_wrong_owner_raises(store_with_two_accounts):
    """get_all_for_account 一樣擋."""
    store, _, acct_b = store_with_two_accounts
    with pytest.raises(PermissionError, match="owner mismatch"):
        store.get_all_for_account(acct_b, expected_owner_user_id=1)


def test_put_acct_wrong_owner_raises(store_with_two_accounts):
    """put_acct: user 1 寫 acct 2 → 擋, account_b cred 不被覆蓋."""
    store, _, acct_b = store_with_two_accounts
    with pytest.raises(PermissionError):
        store.put_acct(acct_b, "password", "MALICIOUS_OVERWRITE", expected_owner_user_id=1)
    # 原密碼還在
    assert store.get_acct(acct_b, "password") == "user-B-secret"


def test_delete_acct_wrong_owner_raises(store_with_two_accounts):
    """delete_acct: user 1 刪 acct 2 cred → 擋, cred 還在."""
    store, _, acct_b = store_with_two_accounts
    with pytest.raises(PermissionError):
        store.delete_acct(acct_b, "password", expected_owner_user_id=1)
    assert store.get_acct(acct_b, "password") == "user-B-secret"


def test_list_fields_acct_wrong_owner_raises(store_with_two_accounts):
    """list_fields_acct 也擋."""
    store, _, acct_b = store_with_two_accounts
    with pytest.raises(PermissionError):
        store.list_fields_acct(acct_b, expected_owner_user_id=1)


def test_nonexistent_account_raises_with_owner_check(store_with_two_accounts):
    """account 不存在 → 帶 owner check 時也 raise PermissionError."""
    store, _, _ = store_with_two_accounts
    with pytest.raises(PermissionError, match="不存在"):
        store.get_acct(99999, "password", expected_owner_user_id=1)
