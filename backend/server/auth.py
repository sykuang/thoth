"""JWT issue/verify + bcrypt hashing (Phase 1).

JWT issue/verify + bcrypt 雜湊（Phase 1）。

Access token = 短命 JWT (HS256)；refresh token = 長命 opaque (DB-backed)。
L9 (2026-06-21): 拆 access / refresh：
  * Access TTL：預設 15 min（短命 → 被偷沒幾分鐘可用）。env `JWT_ACCESS_TTL_MINUTES`
  * Refresh TTL：預設 30 天 rolling（用一次 rotate；30 天沒用才到期）。env `JWT_REFRESH_TTL_DAYS`
  * 舊 env `JWT_TTL_HOURS` 仍支援（被視為 access TTL 小時數，hex compat），但建議改用新 env。

bcrypt cost=12 預設。

Env：
  - JWT_SECRET                — 簽章用 secret（缺就 raise，rotate 要全 user 重登）
  - JWT_ACCESS_TTL_MINUTES    — access token 有效分鐘數（預設 15）
  - JWT_REFRESH_TTL_DAYS      — refresh token 有效天數（預設 30）
  - JWT_TTL_HOURS             — [DEPRECATED] 舊 access TTL（小時），優先順序低於上面
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, UTC
from typing import Any

import bcrypt
from jose import JWTError, jwt

ALGO = "HS256"
# L9: access token 預設 15 分鐘（從 4h 縮短）。refresh token 補上 30 天 rolling。
ACCESS_TTL_MINUTES_DEFAULT = 15
REFRESH_TTL_DAYS_DEFAULT = 30
# 舊 env 名相容（向下相容 Phase 1 ~ L8 的 deployment）
TOKEN_TTL_HOURS_DEFAULT = 4  # 純供 current_ttl_hours() 舊 caller


def current_access_ttl_minutes() -> int:
    """目前生效的 access token TTL（分鐘）。

    優先序：JWT_ACCESS_TTL_MINUTES > JWT_TTL_HOURS*60 > 預設 15。
    非法值（負數 / 非整數）一律回預設。
    """
    raw = os.environ.get("JWT_ACCESS_TTL_MINUTES")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    # Fall back 到舊 JWT_TTL_HOURS（向下相容）
    legacy = os.environ.get("JWT_TTL_HOURS")
    if legacy:
        try:
            h = int(legacy)
            if h > 0:
                return h * 60
        except ValueError:
            pass
    return ACCESS_TTL_MINUTES_DEFAULT


def current_refresh_ttl_days() -> int:
    """目前生效的 refresh token TTL（天）。非法/未設用預設 30。"""
    raw = os.environ.get("JWT_REFRESH_TTL_DAYS")
    if not raw:
        return REFRESH_TTL_DAYS_DEFAULT
    try:
        v = int(raw)
    except ValueError:
        return REFRESH_TTL_DAYS_DEFAULT
    return v if v > 0 else REFRESH_TTL_DAYS_DEFAULT


def current_ttl_hours() -> int:
    """[DEPRECATED] 舊 caller 用；回傳 access TTL（小時）。

    新 code 改用 current_access_ttl_minutes()。保留是因為部分舊 test / 監控 endpoint
    還在引用；移除前要先 grep 全 repo。
    """
    return max(1, current_access_ttl_minutes() // 60)


class AuthError(Exception):
    """Auth-related failure（token 無效 / 過期 / 簽章錯）。"""


def _secret() -> str:
    key = os.environ.get("JWT_SECRET", "")
    if not key:
        raise RuntimeError(
            "JWT_SECRET 未設。產一把：python -c 'import secrets; print(secrets.token_urlsafe(48))'",
        )
    return key


def hash_password(plain: str) -> str:
    """bcrypt 雜湊，cost=12 預設。回 utf-8 string (含 salt + hash)。"""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: int, email: str) -> str:
    """簽 JWT (HS256)，TTL 取自 ENV `JWT_ACCESS_TTL_MINUTES`（預設 15min）。"""
    now = datetime.now(UTC)
    ttl_m = current_access_ttl_minutes()
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl_m)).timestamp()),
    }
    return jwt.encode(claims, _secret(), algorithm=ALGO)


def decode_access_token(token: str) -> dict:
    """驗 JWT；失敗 raise AuthError。"""
    try:
        return jwt.decode(token, _secret(), algorithms=[ALGO])
    except JWTError as e:
        raise AuthError(f"無效的 token: {e}") from e


# ---------------------------------------------------------------------------
# Refresh tokens (L9)
# ---------------------------------------------------------------------------

# Refresh token = 48 byte url-safe random（~64 char）。不是 JWT — 純 opaque token，
# 必須對應 DB row（refresh_tokens.token_hash）才有效。優點：
#   * Server 可即時 revoke（JWT exp 改不了）
#   * Rotation chain 可記錄 + reuse detect
#   * Token 偷走 → revoke family 立即斷
_REFRESH_TOKEN_BYTES = 48


def generate_refresh_token() -> str:
    """產 url-safe 隨機 refresh token 字串（明文，只回 client 一次）。

    用 `secrets.token_urlsafe(48)` ≈ 384 bit entropy，遠超 brute-force 範圍。
    """
    return secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)


def hash_token(raw: str) -> str:
    """sha256(raw_token) → hex string。DB 只存這個，不存明文 token。

    為什麼用 sha256 而不是 bcrypt：refresh token 已是 384 bit 高熵 random，
    不像 password 有 brute-force 風險；用 sha256 換每次 verify O(μs) 而不是 O(200ms)，
    避免 refresh endpoint 變成 DoS amplifier。
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def refresh_token_expiry_iso() -> str:
    """產一個 ISO 8601 UTC 時間字串：now + REFRESH_TTL_DAYS。

    Storage 跟 schema 內所有 timestamp 一致用 `YYYY-MM-DDTHH:MM:SS.fffZ`。
    """
    days = current_refresh_ttl_days()
    target = datetime.now(UTC) + timedelta(days=days)
    return target.strftime("%Y-%m-%dT%H:%M:%S.") + f"{target.microsecond // 1000:03d}Z"
    # 留意：now_iso() 也是同 format，但我們要算 future timestamp，所以自己 format
    # （避免 now_iso 改實作時偷偷影響 expiry 對齊）。
