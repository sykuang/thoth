"""Server-mode Users CRUD（Phase 1）。

只做 INSERT / SELECT，密碼 bcrypt 雜湊。
schema 已在 backend.server.db 建好（users 表）。

Phase 1 single-user mode：第一個 register 的就是 admin (id=1)。
schema 預留 multi-user，邏輯上沒限制誰能 register；要關 register 由 route 層擋。

Phase 9 (2026-06-15): SQL 全 portable — placeholder ?, RETURNING 取 lastrowid，
timestamp 透過 Python now_iso() 傳進去。

Plan B B6 (2026-06-19): 這個 module 本身就是 Repo pattern (server DB users 表)，
caller 看不到 SQL — 跟 creds_store / rules_repo / sync_jobs_repo / preferences_repo
同層級 (不進 db_facade — db_facade 只裝 bank DB 表)。
"""
from __future__ import annotations


from backend.server.auth import hash_password
from backend.server.db import IntegrityError, get_conn, now_iso


class UserExistsError(Exception):
    """同 email 已存在（schema UNIQUE 約束被觸發）。"""


def create_user(email: str, password: str) -> int:
    """新增 user，回 lastrowid。同 email 重複 → raise UserExistsError。"""
    pw_hash = hash_password(password)
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, created_at) "
                "VALUES (?, ?, ?) RETURNING id",
                (email, pw_hash, now_iso()),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("INSERT users 後 RETURNING 為 None")
            return int(row[0])
    except IntegrityError as e:
        raise UserExistsError(f"email already exists: {email}") from e


def _get_user(where_col: str, value: object) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT id, email, password_hash, created_at FROM users WHERE {where_col}=?",
            (value,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "email": row[1],
        "password_hash": row[2],
        "created_at": row[3],
    }


def get_user_by_email(email: str) -> dict | None:
    """以 email 撈一筆 user；查無回 None。"""
    return _get_user("email", email)


def get_user_by_id(user_id: int) -> dict | None:
    """以 id 撈一筆 user；查無回 None。"""
    return _get_user("id", user_id)
