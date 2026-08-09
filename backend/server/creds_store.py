"""Server-mode credential store (Phase 0 -> L5-1).

Phase 0 設計：(user_id, bank, field_name) 三元組定位 ── 一個 user 一銀行只有一組帳密。

Phase L5-1：擴成 account-aware。每個 user 在同一銀行可以有多個 account
(主帳 / 老婆 / 公司)，cred 改掛 account_id。
新 API:
  - AccountsRepo: list/create/delete/rename bank_accounts
  - LocalFernetBackend.put_acct / get_acct / get_all_for_account / list_fields_acct /
    delete_acct / delete_account_cascade

舊 API (put / get / get_all_for_bank / delete / list_fields) 暫保留, 操作的是
舊 bank_credentials 表 ── 老測試還在用、CLI fallback 可能也走這條。L5-3 server API
切換完, 老 API 用 deprecation warning 標記; L5-end 砍 v1 表時一併拔掉。

依賴 env：
  - SERVER_FERNET_KEY  — 32-byte url-safe base64 Fernet key
  - BANK_DATA_ROOT     — backend.server.db.server_db_path() 解析 server.sqlite 位置
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from backend.server.db import get_conn, now_iso


# ============================================================
# bank_accounts CRUD (不涉密文, 純 metadata)
# ============================================================

@dataclass
class BankAccount:
    """一個使用者在一銀行的命名帳號 (主帳 / 老婆 / 公司)。"""
    id: int
    user_id: int
    bank: str
    label: str
    created_at: str
    updated_at: str


class AccountsRepo:
    """bank_accounts 表的 CRUD。獨立 class 因為這層不涉加密。"""

    def list_for_user(self, user_id: int) -> list[BankAccount]:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, bank, label, created_at, updated_at
                FROM bank_accounts
                WHERE user_id=?
                ORDER BY bank, id
                """,
                (user_id,),
            ).fetchall()
        return [BankAccount(*r) for r in rows]

    def list_for_user_bank(self, user_id: int, bank: str) -> list[BankAccount]:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, bank, label, created_at, updated_at
                FROM bank_accounts
                WHERE user_id=? AND bank=?
                ORDER BY id
                """,
                (user_id, bank),
            ).fetchall()
        return [BankAccount(*r) for r in rows]

    def get(self, account_id: int) -> BankAccount | None:
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, bank, label, created_at, updated_at
                FROM bank_accounts WHERE id=?
                """,
                (account_id,),
            ).fetchone()
        return BankAccount(*row) if row else None

    def create(self, user_id: int, bank: str, label: str) -> BankAccount:
        """建一個 account。若 (user_id, bank, label) 已存在 → IntegrityError 由 caller 處理。"""
        ts = now_iso()
        with get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO bank_accounts (user_id, bank, label, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?) RETURNING id
                """,
                (user_id, bank, label, ts, ts),
            )
            row = cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT bank_accounts 後 RETURNING 為 None (DB 異常)")
        new_id = int(row[0])
        got = self.get(new_id)
        assert got is not None, f"create 後找不到 account_id={new_id}"
        return got

    def rename(self, account_id: int, new_label: str) -> BankAccount | None:
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE bank_accounts
                SET label=?, updated_at=?
                WHERE id=?
                """,
                (new_label, now_iso(), account_id),
            )
        return self.get(account_id)

    def delete(self, account_id: int) -> None:
        """刪 account → CASCADE 砍 bank_credentials_v2 內所有 cred。

        sync_jobs.account_id 是 nullable 沒設 FK constraint, 不會 cascade;
        歷史 job 留著做稽核。
        """
        with get_conn() as conn:
            conn.execute("DELETE FROM bank_accounts WHERE id=?", (account_id,))


# ============================================================
# Fernet cred backend (account-aware + 老 v1 API)
# ============================================================

class LocalFernetBackend:
    """SQLite + Fernet 對稱加密。

    L5-1 加入 *_acct 系列 API 以 account_id 定位; 老 v1 API (user_id+bank+field) 仍可用,
    操作的是舊 bank_credentials 表。
    """

    def __init__(self) -> None:
        key = os.environ.get("SERVER_FERNET_KEY", "")
        if not key:
            raise RuntimeError(
                "SERVER_FERNET_KEY 未設。產一把：\n"
                '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"',
            )
        self._fernet = Fernet(key.encode())

    # ------------------------------------------------------------------
    # Account-aware API (L5-1 起所有 server-mode 改用這套)
    #
    # Defense-in-depth (Phase C-Suggestion 2026-06-17):
    # 所有 *_acct API 提供 optional `expected_owner_user_id` 參數。傳了就 inner
    # JOIN bank_accounts.user_id verify owner; 不符合 → raise PermissionError。
    # Router 端 _get_owned_account 已守住，但加這層 secondary check 防：
    #   1. 未來新 caller (script/CLI/cron) 忘記 owner check 直接呼 *_acct;
    #   2. 路由 bug 撞 IDOR 漏 owner check;
    #   3. SQL injection 繞 owner check 透過 plain account_id 直接撈 cred。
    # 沒帶 expected_owner_user_id 時行為跟前一版完全一樣 (caller backward compat)。
    # ------------------------------------------------------------------

    @staticmethod
    def _assert_owner(account_id: int, expected_user_id: int | None) -> None:
        """若 expected_user_id 非 None, 驗 bank_accounts.user_id 對得上, 否則 raise."""
        if expected_user_id is None:
            return
        with get_conn() as conn:
            row = conn.execute(
                "SELECT user_id FROM bank_accounts WHERE id=?",
                (account_id,),
            ).fetchone()
        if row is None:
            raise PermissionError(
                f"account_id={account_id} 不存在 (defense-in-depth)",
            )
        if row[0] != expected_user_id:
            raise PermissionError(
                f"account_id={account_id} owner mismatch: 期望 user_id={expected_user_id}, "
                f"實際 user_id={row[0]} (defense-in-depth)",
            )

    def put_acct(
        self,
        account_id: int,
        field: str,
        plain: str,
        *,
        expected_owner_user_id: int | None = None,
    ) -> None:
        """加密 + upsert 一個欄位到指定 account。"""
        self._assert_owner(account_id, expected_owner_user_id)
        ct = self._fernet.encrypt(plain.encode("utf-8"))
        ts = now_iso()
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO bank_credentials_v2
                    (account_id, field_name, encrypted_val, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id, field_name)
                DO UPDATE SET encrypted_val=excluded.encrypted_val,
                              updated_at=excluded.updated_at
                """,
                (account_id, field, ct, ts, ts),
            )

    def get_acct(
        self,
        account_id: int,
        field: str,
        *,
        expected_owner_user_id: int | None = None,
    ) -> str | None:
        self._assert_owner(account_id, expected_owner_user_id)
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT encrypted_val FROM bank_credentials_v2
                WHERE account_id=? AND field_name=?
                """,
                (account_id, field),
            ).fetchone()
        if not row:
            return None
        try:
            return self._fernet.decrypt(row[0]).decode("utf-8")
        except InvalidToken as e:
            raise RuntimeError(
                f"解密失敗：account_id={account_id} field={field}（key 換過？）",
            ) from e

    def get_all_for_account(
        self,
        account_id: int,
        *,
        expected_owner_user_id: int | None = None,
    ) -> dict[str, str]:
        """一次取一個 account 所有已存欄位 → {field_name: plain}。"""
        self._assert_owner(account_id, expected_owner_user_id)
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT field_name, encrypted_val FROM bank_credentials_v2
                WHERE account_id=?
                """,
                (account_id,),
            ).fetchall()
        out: dict[str, str] = {}
        for fname, ct in rows:
            try:
                out[fname] = self._fernet.decrypt(ct).decode("utf-8")
            except InvalidToken as e:
                raise RuntimeError(
                    f"解密失敗：account_id={account_id} field={fname}（key 換過？）",
                ) from e
        return out

    def delete_acct(
        self,
        account_id: int,
        field: str,
        *,
        expected_owner_user_id: int | None = None,
    ) -> None:
        self._assert_owner(account_id, expected_owner_user_id)
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM bank_credentials_v2 WHERE account_id=? AND field_name=?",
                (account_id, field),
            )

    def list_fields_acct(
        self,
        account_id: int,
        *,
        expected_owner_user_id: int | None = None,
    ) -> list[str]:
        self._assert_owner(account_id, expected_owner_user_id)
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT field_name FROM bank_credentials_v2
                WHERE account_id=?
                ORDER BY field_name
                """,
                (account_id,),
            ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # 老 v1 API (Phase 0~L4 留下; 老測試 + 老 CLI 還在用)
    # 警告: 同一 user+bank 只能存一份 cred, 不支援多帳號
    # ------------------------------------------------------------------

    def put(self, user_id: int, bank: str, field: str, plain: str) -> None:
        ct = self._fernet.encrypt(plain.encode("utf-8"))
        ts = now_iso()
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO bank_credentials
                    (user_id, bank, field_name, encrypted_val, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, bank, field_name)
                DO UPDATE SET encrypted_val=excluded.encrypted_val,
                              updated_at=excluded.updated_at
                """,
                (user_id, bank, field, ct, ts, ts),
            )

    def get(self, user_id: int, bank: str, field: str) -> str | None:
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT encrypted_val FROM bank_credentials
                WHERE user_id=? AND bank=? AND field_name=?
                """,
                (user_id, bank, field),
            ).fetchone()
        if not row:
            return None
        try:
            return self._fernet.decrypt(row[0]).decode("utf-8")
        except InvalidToken as e:
            raise RuntimeError(
                f"解密失敗：user_id={user_id} bank={bank} field={field}（key 換過？）",
            ) from e

    def get_all_for_bank(self, user_id: int, bank: str) -> dict[str, str]:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT field_name, encrypted_val FROM bank_credentials
                WHERE user_id=? AND bank=?
                """,
                (user_id, bank),
            ).fetchall()
        out: dict[str, str] = {}
        for fname, ct in rows:
            try:
                out[fname] = self._fernet.decrypt(ct).decode("utf-8")
            except InvalidToken as e:
                raise RuntimeError(
                    f"解密失敗：user_id={user_id} bank={bank} field={fname}（key 換過？）",
                ) from e
        return out

    def delete(self, user_id: int, bank: str, field: str) -> None:
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM bank_credentials WHERE user_id=? AND bank=? AND field_name=?",
                (user_id, bank, field),
            )

    def list_fields(self, user_id: int, bank: str) -> list[str]:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT field_name FROM bank_credentials
                WHERE user_id=? AND bank=?
                ORDER BY field_name
                """,
                (user_id, bank),
            ).fetchall()
        return [r[0] for r in rows]


def list_account_metadata(user_id: int) -> list[dict]:
    """Return non-secret bank account slots with credential field presence."""
    repo = AccountsRepo()
    store = LocalFernetBackend()
    out: list[dict] = []
    for account in repo.list_for_user(user_id):
        fields = store.list_fields_acct(
            account.id,
            expected_owner_user_id=user_id,
        )
        out.append({
            "id": account.id,
            "bank": account.bank,
            "label": account.label,
            "created_at": account.created_at,
            "updated_at": account.updated_at,
            "has_creds": bool(fields),
            "fields_set": fields,
        })
    return out
