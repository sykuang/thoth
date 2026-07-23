"""Phase 1 — /auth/register, /auth/login routes。
L9 (2026-06-21) — 加 /auth/refresh, /auth/logout，配 refresh-token rotation。

/auth/me 已在 app.py 直接掛（避免循環 import）。

Endpoints:
  POST /auth/register   {email, password}                     → 201 TokenPair + user_id + email
  POST /auth/login      OAuth2PasswordRequestForm             → 200 TokenPair
  POST /auth/refresh    {refresh_token}                       → 200 TokenPair
  POST /auth/logout     {refresh_token}                       → 204
"""
from __future__ import annotations

import contextlib
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field

from backend.server.auth import (
    create_access_token,
    current_access_ttl_minutes,
    verify_password,
)
from backend.server.refresh_tokens import (
    RefreshTokenError,
    RefreshTokenReuse,
    issue as issue_refresh,
    revoke as revoke_refresh,
    rotate as rotate_refresh,
)
from backend.server.security import (
    enforce_login_rate_limit,
    login_limiter,
    register_constant_time_delay,
)
from backend.server.seed_rules import seed_default_rules
from backend.server.users import (
    UserExistsError,
    create_user,
    get_user_by_email,
    get_user_by_id,
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class RegisterBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)


class TokenPair(BaseModel):
    """L9: 統一 login/register/refresh 回的 token 結構。

    `token` 跟 `access_token` 都填 access JWT，純為了向下相容舊 client（Phase 1
    client 用 `token`、Phase 4 之後用 `access_token`）。新 client 只看 `access_token`。
    """
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # access token 剩餘秒數，方便 client 排 silent refresh


class RegisterResp(TokenPair):
    """Register 多回 user_id + email — 老 client 依賴。"""
    token: str  # = access_token，向下相容
    user_id: int
    email: EmailStr


class LogoutBody(BaseModel):
    refresh_token: str = Field(min_length=1)


class RefreshBody(BaseModel):
    refresh_token: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_token_pair(user_id: int, email: str, request: Request) -> tuple[str, str, int]:
    """簽 access + 發新 refresh family，回 (access, refresh_raw, expires_in_seconds)。"""
    access = create_access_token(user_id=user_id, email=email)
    ua = request.headers.get("user-agent", "") if request else ""
    ip = _client_ip(request) if request else ""
    refresh = issue_refresh(user_id=user_id, user_agent=ua or None, ip_address=ip or None)
    expires_in = current_access_ttl_minutes() * 60
    return access, refresh, expires_in


def _client_ip(request: Request | None) -> str:
    if not request:
        return ""
    # 跟 security.py 同邏輯：先 X-Forwarded-For，再 client.host
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisterResp,
)
def register(
    body: RegisterBody,
    request: Request,
    _ip: str = Depends(enforce_login_rate_limit),
) -> RegisterResp:
    # W3 (a): 共用 login_limiter 池 IP 失敗 5 次即鎖 30 分鐘（攻擊者枚舉 email 會被擋）
    # W3 (b): 不論成功或 409，都在回 response 前加一段固定延遲，掩蓋 bcrypt 200ms vs 即返的 timing 差
    started = time.monotonic()
    delay = register_constant_time_delay()
    try:
        try:
            uid = create_user(email=body.email, password=body.password)
        except UserExistsError:
            # W3: 409 也算「失敗」加進 IP 計數；達門檻直接 429（同 login 一致）
            fails = login_limiter.record_failure(_ip)
            if fails >= login_limiter.max_failures:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    f"註冊失敗過多，已暫時鎖定 {login_limiter.lockout_seconds // 60} 分鐘",
                ) from None
            raise HTTPException(status.HTTP_409_CONFLICT, f"此 email 已註冊過: {body.email}") from None
        # Phase 5.1: seed default categorization rules（失敗不該擋 register）
        with contextlib.suppress(Exception):
            seed_default_rules(user_id=uid)
        access, refresh, expires_in = _build_token_pair(uid, body.email, request)
        return RegisterResp(
            access_token=access,
            refresh_token=refresh,
            expires_in=expires_in,
            token=access,  # 向下相容 Phase 1 client 用 r.token
            user_id=uid,
            email=body.email,
        )
    finally:
        # constant-time-ish：把整段處理時間補到至少 `delay` 秒
        if delay > 0:
            elapsed = time.monotonic() - started
            remaining = delay - elapsed
            if remaining > 0:
                time.sleep(remaining)


@router.post("/login", response_model=TokenPair)
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
) -> TokenPair:
    # L8.5: 鎖定窗內直接 429（不算 bcrypt cost）
    ip = enforce_login_rate_limit(request)
    user = get_user_by_email(form.username)
    if not user or not verify_password(form.password, user["password_hash"]):
        fails = login_limiter.record_failure(ip)
        # 給 client 知道還剩幾次（沒到門檻才提示，到了會被下次 enforce 擋）
        max_fails = login_limiter.max_failures
        remaining = max(0, max_fails - fails)
        if remaining == 0:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"登入失敗過多，已暫時鎖定 {login_limiter.lockout_seconds // 60} 分鐘",
            )
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"帳號或密碼錯誤（剩餘 {remaining} 次嘗試）",
        )
    login_limiter.record_success(ip)
    access, refresh, expires_in = _build_token_pair(user["id"], user["email"], request)
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
    )


@router.post("/refresh", response_model=TokenPair)
def refresh(
    body: RefreshBody,
    request: Request,
) -> TokenPair:
    """旋轉 refresh token：用舊的換 (new_access, new_refresh)；舊的立即失效。

    錯誤都回 401：
      * 無效 / 已過期 → "請重新登入"（不透露細節）
      * Reuse 偵測（已 revoke 再被用）→ revoke 整個 family，也回 "請重新登入"
        (不告訴攻擊者「被偵測」，否則他知道要換策略)
    """
    ua = request.headers.get("user-agent", "")
    ip = _client_ip(request)
    try:
        uid, new_refresh = rotate_refresh(
            body.refresh_token,
            user_agent=ua or None,
            ip_address=ip or None,
        )
    except RefreshTokenReuse:
        # 故意跟一般失效同樣回應，避免攻擊者推斷 server 偵測機制
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "請重新登入") from None
    except RefreshTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "請重新登入") from None

    user = get_user_by_id(uid)
    if not user:
        # 極少數情況：rotate 跟 user 刪除 race
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "請重新登入")
    access = create_access_token(user_id=uid, email=user["email"])
    expires_in = current_access_ttl_minutes() * 60
    return TokenPair(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=expires_in,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(body: LogoutBody) -> Response:
    """主動撤銷 refresh token。Idempotent：token 不存在 / 已 revoked 也回 204。"""
    revoke_refresh(body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
