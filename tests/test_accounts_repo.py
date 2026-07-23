"""Phase L5-1 tests: bank_accounts + bank_credentials_v2 + from_account.

涵蓋:
- AccountsRepo CRUD (list/create/delete/rename + duplicate-label IntegrityError)
- LocalFernetBackend 新 *_acct API (put/get/list/delete) + cascade
- core.creds.BankCreds.from_account (成功 + 缺欄位)
- core.creds.BankCreds.load() account-mode 透過 BANK_CRAWLER_ACCOUNT_ID env
- L5-1 自動 migration: 舊 v1 row → 新 v2 + 預設 account
"""
from __future__ import annotations

import importlib
import sqlite3

import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """tmp_path + fresh Fernet key per test, 確保 db 隔離。"""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("SERVER_FERNET_KEY", Fernet.generate_key().decode())
    import backend.server.db as db_mod
    import backend.server.creds_store as cs_mod
    importlib.reload(db_mod)
    importlib.reload(cs_mod)
    yield


def _make_user(user_email: str = "test@example.com") -> int:
    """直接插一個 user_id 進 users 表，回傳 id。"""
    from backend.server.db import get_conn

    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO users (email, password_hash, created_at)
            VALUES (?, '$dummy$', datetime('now'))
            """,
            (user_email,),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid


# ============================================================
# AccountsRepo
# ============================================================

def test_accounts_create_and_get(isolated_db):
    from backend.server.creds_store import AccountsRepo

    user_id = _make_user()
    repo = AccountsRepo()
    a = repo.create(user_id=user_id, bank="cathay", label="主帳")
    assert a.id > 0
    assert a.user_id == user_id
    assert a.bank == "cathay"
    assert a.label == "主帳"
    assert a.created_at  # datetime('now') 不會空

    got = repo.get(a.id)
    assert got is not None
    assert got.label == "主帳"


def test_accounts_list_for_user(isolated_db):
    from backend.server.creds_store import AccountsRepo

    u1 = _make_user("u1@example.com")
    u2 = _make_user("u2@example.com")
    repo = AccountsRepo()
    repo.create(u1, "cathay", "主帳")
    repo.create(u1, "cathay", "老婆")
    repo.create(u1, "ctbc", "主帳")
    repo.create(u2, "cathay", "主帳")

    u1_accts = repo.list_for_user(u1)
    assert len(u1_accts) == 3
    assert {a.bank for a in u1_accts} == {"cathay", "ctbc"}

    u1_cathay = repo.list_for_user_bank(u1, "cathay")
    assert len(u1_cathay) == 2
    assert {a.label for a in u1_cathay} == {"主帳", "老婆"}


def test_accounts_duplicate_label_raises(isolated_db):
    from backend.server.creds_store import AccountsRepo

    user_id = _make_user()
    repo = AccountsRepo()
    repo.create(user_id, "cathay", "主帳")
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(user_id, "cathay", "主帳")


def test_accounts_rename(isolated_db):
    from backend.server.creds_store import AccountsRepo

    user_id = _make_user()
    repo = AccountsRepo()
    a = repo.create(user_id, "cathay", "主帳")
    renamed = repo.rename(a.id, "新名字")
    assert renamed is not None
    assert renamed.label == "新名字"


def test_accounts_delete_cascades_v2_creds(isolated_db):
    """刪 account → bank_credentials_v2 內所屬 cred 都該 cascade 砍掉。"""
    from backend.server.creds_store import AccountsRepo, LocalFernetBackend

    user_id = _make_user()
    repo = AccountsRepo()
    backend = LocalFernetBackend()
    a = repo.create(user_id, "cathay", "主帳")
    backend.put_acct(a.id, "password", "p@ss")
    backend.put_acct(a.id, "user_id", "myid")
    assert len(backend.list_fields_acct(a.id)) == 2

    repo.delete(a.id)
    assert repo.get(a.id) is None
    assert backend.list_fields_acct(a.id) == []  # cascade 後沒有 cred


# ============================================================
# LocalFernetBackend account-aware API
# ============================================================

def test_put_get_acct_roundtrip(isolated_db):
    from backend.server.creds_store import AccountsRepo, LocalFernetBackend

    user_id = _make_user()
    repo = AccountsRepo()
    backend = LocalFernetBackend()
    a = repo.create(user_id, "cathay", "主帳")

    backend.put_acct(a.id, "password", "p@ssw0rd!")
    assert backend.get_acct(a.id, "password") == "p@ssw0rd!"

    # upsert
    backend.put_acct(a.id, "password", "newpw")
    assert backend.get_acct(a.id, "password") == "newpw"


def test_get_all_for_account(isolated_db):
    from backend.server.creds_store import AccountsRepo, LocalFernetBackend

    user_id = _make_user()
    repo = AccountsRepo()
    backend = LocalFernetBackend()
    a = repo.create(user_id, "cathay", "主帳")

    backend.put_acct(a.id, "cust_id", "B123456789")
    backend.put_acct(a.id, "user_id", "testuser")
    backend.put_acct(a.id, "password", "secret")

    vals = backend.get_all_for_account(a.id)
    assert vals == {"cust_id": "B123456789", "user_id": "testuser", "password": "secret"}


def test_delete_acct_field(isolated_db):
    from backend.server.creds_store import AccountsRepo, LocalFernetBackend

    user_id = _make_user()
    repo = AccountsRepo()
    backend = LocalFernetBackend()
    a = repo.create(user_id, "cathay", "主帳")
    backend.put_acct(a.id, "password", "x")
    backend.delete_acct(a.id, "password")
    assert backend.get_acct(a.id, "password") is None


def test_accounts_isolation_across_accounts(isolated_db):
    """同 user 同 bank 的兩個 account, cred 互不可見。"""
    from backend.server.creds_store import AccountsRepo, LocalFernetBackend

    user_id = _make_user()
    repo = AccountsRepo()
    backend = LocalFernetBackend()
    main = repo.create(user_id, "cathay", "主帳")
    wife = repo.create(user_id, "cathay", "老婆")
    backend.put_acct(main.id, "password", "main_pw")
    backend.put_acct(wife.id, "password", "wife_pw")
    assert backend.get_acct(main.id, "password") == "main_pw"
    assert backend.get_acct(wife.id, "password") == "wife_pw"


# ============================================================
# core/creds.from_account
# ============================================================

def test_from_account_success(isolated_db):
    from backend.core.creds import CathayCreds
    from backend.server.creds_store import AccountsRepo, LocalFernetBackend

    user_id = _make_user()
    repo = AccountsRepo()
    backend = LocalFernetBackend()
    a = repo.create(user_id, "cathay", "主帳")
    backend.put_acct(a.id, "cust_id", "A123")
    backend.put_acct(a.id, "user_id", "testuser")
    backend.put_acct(a.id, "password", "test-pw-strong")

    creds = CathayCreds.from_account(a.id)
    assert creds.cust_id == "A123"
    assert creds.user_id == "testuser"
    assert creds.password == "test-pw-strong"


def test_from_account_missing_field_raises(isolated_db):
    from backend.core.creds import CathayCreds, CredError
    from backend.server.creds_store import AccountsRepo, LocalFernetBackend

    user_id = _make_user()
    repo = AccountsRepo()
    backend = LocalFernetBackend()
    a = repo.create(user_id, "cathay", "主帳")
    backend.put_acct(a.id, "password", "pw")  # 只填密碼, 缺 cust_id + user_id

    with pytest.raises(CredError) as exc:
        CathayCreds.from_account(a.id)
    msg = str(exc.value)
    assert "cust_id" in msg or "user_id" in msg


def test_load_account_mode_via_env(isolated_db, monkeypatch):
    """BANK_CRAWLER_ACCOUNT_ID 設了 → load() 走 from_account 路徑。"""
    from backend.core.creds import CathayCreds
    from backend.server.creds_store import AccountsRepo, LocalFernetBackend

    user_id = _make_user()
    repo = AccountsRepo()
    backend = LocalFernetBackend()
    a = repo.create(user_id, "cathay", "主帳")
    backend.put_acct(a.id, "cust_id", "A123")
    backend.put_acct(a.id, "user_id", "kenuser")
    backend.put_acct(a.id, "password", "test-pw-strong")

    monkeypatch.setenv("BANK_CRAWLER_ACCOUNT_ID", str(a.id))
    monkeypatch.delenv("BANK_CRAWLER_USER_ID", raising=False)

    creds = CathayCreds.load()
    assert creds.cust_id == "A123"


# ============================================================
# L5-1 自動 migration
# ============================================================

def test_v1_to_v2_migration_on_boot(isolated_db):
    """舊 bank_credentials row → 自動建 預設 account + copy 到 v2。"""
    from backend.server.creds_store import AccountsRepo, LocalFernetBackend
    from backend.server.db import get_conn

    user_id = _make_user()
    backend = LocalFernetBackend()
    # 用老 API 寫進 v1 表
    backend.put(user_id, "cathay", "password", "old_pw")
    backend.put(user_id, "cathay", "user_id", "old_user")
    backend.put(user_id, "ctbc", "national_id", "old_id")

    # 重新 trigger schema (透過 get_conn 開連線就會跑 _ensure_schema + migration)
    with get_conn():
        pass

    repo = AccountsRepo()
    cathay_accts = repo.list_for_user_bank(user_id, "cathay")
    ctbc_accts = repo.list_for_user_bank(user_id, "ctbc")
    assert len(cathay_accts) == 1
    assert cathay_accts[0].label == "預設"
    assert len(ctbc_accts) == 1
    assert ctbc_accts[0].label == "預設"

    # v2 表也有對應 cred
    vals_cathay = backend.get_all_for_account(cathay_accts[0].id)
    assert vals_cathay == {"password": "old_pw", "user_id": "old_user"}
    vals_ctbc = backend.get_all_for_account(ctbc_accts[0].id)
    assert vals_ctbc == {"national_id": "old_id"}


def test_migration_idempotent(isolated_db):
    """跑兩次 schema 不該重複建 account 或 dup cred。"""
    from backend.server.creds_store import AccountsRepo, LocalFernetBackend
    from backend.server.db import get_conn

    user_id = _make_user()
    backend = LocalFernetBackend()
    backend.put(user_id, "cathay", "password", "pw")

    # 第一次 trigger
    with get_conn():
        pass
    # 第二次 trigger
    with get_conn():
        pass
    # 第三次 trigger
    with get_conn():
        pass

    repo = AccountsRepo()
    accts = repo.list_for_user_bank(user_id, "cathay")
    assert len(accts) == 1
    fields = backend.list_fields_acct(accts[0].id)
    assert fields == ["password"]
