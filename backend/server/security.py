"""Security middleware (Phase L8.5)。

兩道防線（疊在 JWT auth 之外）：

1. **X-API-Key**（可選）：
   - 設了 env `SERVER_API_KEY` → 所有 request 都得帶 `X-API-Key: <key>`
     header（或 query `?api_key=...`，行動裝置 fallback 用）才放行
   - 沒設 env → middleware no-op（dev / 內網 OK）
   - 例外路徑：`/healthz`（容器健康檢查、reverse proxy 用）

2. **Login rate limit**（永遠開）：
   - `POST /auth/login` 失敗計次，**單一 IP** 連續 5 次密碼錯 → 鎖 30 分鐘
   - in-memory dict（重啟即清；單機家用 server 夠用，不需要 Redis）
   - 成功 login 立刻清掉該 IP 的計數
   - 鎖定期間直接回 429（不再驗密碼，省 bcrypt cost）

Env：
  - `SERVER_API_KEY`            — 設了才啟 API key 檢查；建議 32+ 字隨機
  - `LOGIN_MAX_FAILURES`        — 連續失敗門檻（預設 5）
  - `LOGIN_LOCKOUT_SECONDS`     — 鎖定秒數（預設 1800 = 30 min）

設計取捨：
  - 為何「鎖 IP」不「鎖帳號」：銀行端鎖帳號是因為他們知道帳號真實存在；
    這裡如果鎖帳號，攻擊者可拿任意 email 鎖死真實用戶（DoS 自家）。
    鎖 IP 雖然 NAT 後面會誤傷，但家用 server 場景 OK。
  - 為何不用 slowapi / fastapi-limiter：避免引入額外 dep；
    in-memory 30 行 code 就夠，未來真要 prod 再換 Redis 版。
"""
from __future__ import annotations

import os
import secrets
import time
from threading import RLock

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# ─── X-API-Key middleware ───────────────────────────────────────────────────────

# 不檢 API key 的路徑（健康檢查、CORS preflight）
_API_KEY_EXEMPT_PATHS = {"/healthz", "/api/healthz"}


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Server 級 shared secret。設了 env 才啟用，沒設就 no-op。

    優先讀 `X-API-Key` header；fallback query `?api_key=`（行動端 image
    preview / 開新 tab 那種不好塞 header 的情境用）。
    """

    async def dispatch(self, request: Request, call_next):
        bootstrap_only = os.environ.get("THOTH_BOOTSTRAP_NETWORK_ONLY", "").strip() in (
            "1",
            "true",
            "True",
        )
        if bootstrap_only and request.url.path not in _API_KEY_EXEMPT_PATHS:
            return Response(
                content='{"detail":"network bootstrap in progress"}',
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                media_type="application/json",
            )

        api_key = os.environ.get("SERVER_API_KEY", "").strip()
        if not api_key:
            return await call_next(request)

        # OPTIONS (CORS preflight) 必放（瀏覽器不會帶 X-API-Key 在 preflight）
        if request.method == "OPTIONS":
            return await call_next(request)

        if request.url.path in _API_KEY_EXEMPT_PATHS:
            return await call_next(request)

        provided = (
            request.headers.get("X-API-Key", "").strip()
            or request.query_params.get("api_key", "").strip()
        )
        # C8: 用 secrets.compare_digest 防 timing attack（兩字串等長/不等長都 constant-time）
        if not secrets.compare_digest(provided, api_key):
            return Response(
                content='{"detail":"Invalid or missing X-API-Key"}',
                status_code=status.HTTP_401_UNAUTHORIZED,
                media_type="application/json",
            )
        return await call_next(request)


# ─── Login rate limit ──────────────────────────────────────────────────────────


class LoginRateLimiter:
    """單一 IP 失敗計次 + 時窗鎖定。Thread-safe (RLock)。"""

    def __init__(self) -> None:
        self._fail_count: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = RLock()

    @property
    def max_failures(self) -> int:
        try:
            return int(os.environ.get("LOGIN_MAX_FAILURES", "5"))
        except ValueError:
            return 5

    @property
    def lockout_seconds(self) -> int:
        try:
            return int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "1800"))
        except ValueError:
            return 1800

    def check_locked(self, ip: str) -> int | None:
        """還在鎖定窗 → 回剩餘秒數；否則 None。同步清過期鎖。"""
        now = time.time()
        with self._lock:
            until = self._locked_until.get(ip)
            if until is None:
                return None
            if now >= until:
                # 鎖期已過 — 清掉計數器讓 user 重來
                self._locked_until.pop(ip, None)
                self._fail_count.pop(ip, None)
                return None
            return int(until - now)

    def record_failure(self, ip: str) -> int:
        """回傳本次失敗後的累計次數。達門檻時自動上鎖。"""
        with self._lock:
            n = self._fail_count.get(ip, 0) + 1
            self._fail_count[ip] = n
            if n >= self.max_failures:
                self._locked_until[ip] = time.time() + self.lockout_seconds
            return n

    def record_success(self, ip: str) -> None:
        """成功 login 立刻清計數。"""
        with self._lock:
            self._fail_count.pop(ip, None)
            self._locked_until.pop(ip, None)

    def reset(self) -> None:
        """測試用：全清。"""
        with self._lock:
            self._fail_count.clear()
            self._locked_until.clear()


# Singleton（process-wide；多 worker 場景請改 Redis）
login_limiter = LoginRateLimiter()


def get_client_ip(request: Request) -> str:
    """信任 reverse proxy 的 X-Forwarded-For 第一個 IP；否則用 client.host。

    註：production 走 nginx/caddy 時要設好 trusted_proxies，否則 client 能偽造。
    家用單機 (uvicorn 直曝) 沒這個問題，request.client.host 一定真。
    """
    xff = request.headers.get("X-Forwarded-For", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    if request.client is None:
        return "unknown"
    return request.client.host or "unknown"


def enforce_login_rate_limit(request: Request) -> str:
    """檢查 IP 是否被鎖，鎖了就 raise 429。沒鎖就回 IP 給 caller 後續記錄。"""
    ip = get_client_ip(request)
    remaining = login_limiter.check_locked(ip)
    if remaining is not None:
        # 用 minutes 顯示比較友善
        mins = (remaining + 59) // 60
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登入嘗試過多，請於 {mins} 分鐘後再試",
            headers={"Retry-After": str(remaining)},
        )
    return ip


# ─── Register IP rate limit + constant-time delay (W3) ─────────────────────────
#
# Register 走 `enforce_login_rate_limit` 同一個池（per-IP 5 次失敗即鎖 30 分鐘）。
# 為何重用：簡化 deps、單機家用 server 同 IP 不該又狂 register 又狂 login；
#          攻擊者枚舉 email（看 409）跟暴力 login 用同 IP 池一起鎖才合理。
#
# Constant-time delay：register 成功路徑 (~bcrypt 200ms) vs 409 即返路徑
# 差太多，攻擊者可從 latency 推 email 存在性。預設加 1.0s sleep 壓掉差距。
# 測試用 ENV `REGISTER_DELAY_SECONDS=0` 關掉避免拖慢 pytest。
#
# Env：
#   - REGISTER_DELAY_SECONDS — 浮點秒數，預設 1.0。設 0 完全關閉（測試用）


def register_constant_time_delay() -> float:
    """讀 ENV 拿延遲秒數；非法值/未設用 1.0；設 0 表示關閉。"""
    raw = os.environ.get("REGISTER_DELAY_SECONDS", "1.0")
    try:
        v = float(raw)
    except ValueError:
        return 1.0
    return max(0.0, v)
