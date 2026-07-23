"""Refresh tokens repository (L9, 2026-06-21).

Refresh token = 長命 opaque token，存進 `refresh_tokens` 表。
跟 access token (JWT 15 min) 配對運作：

  POST /auth/login    → 簽 access + 發 refresh
  POST /auth/refresh  → 拿舊 refresh 換 (new_access, new_refresh)；舊 refresh 立即 revoke
  POST /auth/logout   → revoke current refresh

Rotation chain:
  refresh A (family X) → rotate → refresh B (family X, A.replaced_by = hash(B))
                       → rotate → refresh C (family X, B.replaced_by = hash(C))
  舊 token A/B 一律 mark revoked_at；client 只該持有最新的 C。

Reuse detection (OAuth 2.0 best practice):
  如果攻擊者偷了 A 跑去 /refresh，server 看到 A 已 revoked → 代表 token 被偷
  → revoke 整 family X（B、C 都失效）→ 真正使用者下次 access 過期會被踢回 login
  → 攻擊者也拿不到任何新 token。

Storage:
  * token_hash = sha256(raw_token)；DB 永遠不存明文
  * raw_token 只在 issue/rotate 時回傳給 client 一次
  * client 把 raw_token 存 Keychain (iOS) / localStorage (web)
"""
from __future__ import annotations

import uuid
from typing import Any

from backend.server.auth import (
    generate_refresh_token,
    hash_token,
    refresh_token_expiry_iso,
)
from backend.server.db import get_conn, now_iso


class RefreshTokenError(Exception):
    """Refresh token 無效 / 過期 / 已 revoke / reuse 攻擊偵測。"""


class RefreshTokenReuse(RefreshTokenError):
    """Token 已 revoked 卻又被使用 — 代表被偷，已 revoke 整 family。

    Caller 應該回 401 並提示 user 重登（不要透露 reuse detection 細節給攻擊者）。
    """


def issue(
    user_id: int,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> str:
    """發一張新 refresh token (建新 family)。回傳明文 raw token，DB 只存 hash。

    用於 /auth/login 與 /auth/register 時 — 此時沒有舊 token，所以開新 family。
    """
    raw = generate_refresh_token()
    family_id = str(uuid.uuid4())
    _insert(
        user_id=user_id,
        token_hash=hash_token(raw),
        family_id=family_id,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    return raw


def rotate(
    raw_token: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[int, str]:
    """旋轉 refresh token：驗舊的 → 發新的 → 舊的 mark revoked。

    回傳 (user_id, new_raw_token)。
    舊 token：
      * 不存在 → RefreshTokenError（無效）
      * 已 revoked → RefreshTokenReuse（reuse 攻擊，已 revoke family）
      * 已 expired → RefreshTokenError + 順手 revoke 該 token（不擴及 family）
      * 合法 → 發新 token、舊 mark revoked + replaced_by
    """
    old_hash = hash_token(raw_token)
    row = _get_by_hash(old_hash)
    if not row:
        raise RefreshTokenError("refresh token 無效")

    user_id = int(row["user_id"])
    family_id = row["family_id"]
    now = now_iso()

    # 1) Reuse detection: 已 revoked 又被用 → revoke 整 family
    if row["revoked_at"] is not None:
        _revoke_family(family_id, now)
        raise RefreshTokenReuse(
            "refresh token 已撤銷後再次使用，已撤銷整個 token family",
        )

    # 2) Expired: 順手 revoke，不擴及 family（單純過期，不是攻擊）
    if row["expires_at"] <= now:
        _mark_revoked(old_hash, now, replaced_by=None)
        raise RefreshTokenError("refresh token 已過期")

    # 3) Happy path: 發新 token, 舊 mark revoked + chain
    new_raw = generate_refresh_token()
    new_hash = hash_token(new_raw)
    _insert(
        user_id=user_id,
        token_hash=new_hash,
        family_id=family_id,  # 同家族
        user_agent=user_agent,
        ip_address=ip_address,
    )
    _mark_revoked(old_hash, now, replaced_by=new_hash)
    return user_id, new_raw


def revoke(raw_token: str) -> bool:
    """主動 revoke 一張 refresh token (POST /auth/logout)。

    回 True 表示有 revoke 到；False 表示 token 不存在或已 revoked（皆 idempotent OK）。
    不擴及 family — logout 是正常行為不是攻擊。
    """
    token_hash = hash_token(raw_token)
    row = _get_by_hash(token_hash)
    if not row or row["revoked_at"] is not None:
        return False
    _mark_revoked(token_hash, now_iso(), replaced_by=None)
    return True


def revoke_all_for_user(user_id: int) -> int:
    """Force logout 一個 user 的所有 active refresh tokens。

    用途：(a) 改密碼後清掉所有 device session；(b) 管理員操作。
    回傳 revoked 筆數。
    """
    with get_conn() as con:
        cur = con.execute(
            "UPDATE refresh_tokens SET revoked_at = ? "
            "WHERE user_id = ? AND revoked_at IS NULL",
            (now_iso(), user_id),
        )
        try:
            return cur.rowcount or 0
        except AttributeError:
            return 0


def prune_expired(now: str | None = None) -> int:
    """刪掉所有 expired tokens（含已 revoked）。一般用 cron 跑。

    回傳刪掉的筆數。
    """
    cutoff = now or now_iso()
    with get_conn() as con:
        cur = con.execute(
            "DELETE FROM refresh_tokens WHERE expires_at <= ?",
            (cutoff,),
        )
        try:
            return cur.rowcount or 0
        except AttributeError:
            return 0


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _insert(
    *,
    user_id: int,
    token_hash: str,
    family_id: str,
    user_agent: str | None,
    ip_address: str | None,
) -> None:
    now = now_iso()
    expires = refresh_token_expiry_iso()
    with get_conn() as con:
        con.execute(
            "INSERT INTO refresh_tokens "
            "(user_id, token_hash, family_id, issued_at, expires_at, "
            " revoked_at, replaced_by, user_agent, ip_address) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                user_id,
                token_hash,
                family_id,
                now,
                expires,
                (user_agent or "")[:512] or None,
                (ip_address or "")[:64] or None,
            ),
        )


def _get_by_hash(token_hash: str) -> dict[str, Any] | None:
    with get_conn() as con:
        cur = con.execute(
            "SELECT id, user_id, token_hash, family_id, issued_at, "
            "       expires_at, revoked_at, replaced_by "
            "FROM refresh_tokens WHERE token_hash = ?",
            (token_hash,),
        )
        row = cur.fetchone()
    if not row:
        return None
    # Normalize sqlite Row / psycopg tuple to dict
    if hasattr(row, "keys"):
        keys = list(row.keys())
        return {k: row[k] for k in keys}
    keys = ["id", "user_id", "token_hash", "family_id", "issued_at",
            "expires_at", "revoked_at", "replaced_by"]
    return dict(zip(keys, row, strict=False))


def _mark_revoked(token_hash: str, when: str, replaced_by: str | None) -> None:
    with get_conn() as con:
        con.execute(
            "UPDATE refresh_tokens SET revoked_at = ?, replaced_by = ? "
            "WHERE token_hash = ?",
            (when, replaced_by, token_hash),
        )


def _revoke_family(family_id: str, when: str) -> int:
    """Revoke 整個 family（含已 revoked 的也不動）。回 affected count。"""
    with get_conn() as con:
        cur = con.execute(
            "UPDATE refresh_tokens SET revoked_at = ? "
            "WHERE family_id = ? AND revoked_at IS NULL",
            (when, family_id),
        )
        try:
            return cur.rowcount or 0
        except AttributeError:
            return 0
