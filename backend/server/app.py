"""FastAPI app entrypoint（Phase 1）。

Bootstrap：
  uvicorn backend.server.app:app --host 0.0.0.0 --port 8000

Env：
  - JWT_SECRET           — JWT 簽章 secret（必設）
  - SERVER_FERNET_KEY    — 憑證 Fernet key（用到 credentials/sync 才需要）
  - BANK_DATA_ROOT       — server.sqlite 根目錄（預設 backend/data）

路由：
  - GET  /healthz                 健康檢查（無需 auth）
  - GET  /auth/me                 拿目前 user（需 Bearer token）
  - POST /auth/register           開帳號（Phase 1 single-user mode）
  - POST /auth/login              拿 token
  - GET  /credentials             列各 bank 已設欄位
  - PUT  /credentials/{bank}      寫一個或多個欄位
  - DELETE /credentials/{bank}    清掉整 bank
  - DELETE /credentials/{bank}/{field}  清單欄位
  - POST /sync/{bank}             觸發 sync job（背景 thread）
  - GET  /sync/jobs               近 50 筆 job
  - GET  /sync/jobs/{id}          單 job 狀態
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

# Auto-load .env from project root（uvicorn 不自動讀，這裡補上）
# python-dotenv 找到的 var 已 set 在 env 時不覆蓋（override=False），
# 保留 shell export / docker env / kubernetes secret 的優先權。
# 2026-06-14 拆分後三層 .env (見 wiki: thoth-env-three-layer-split-lesson):
#   1. backend/server/.env  — server runtime secrets (本檔主要載這個)
#   2. cli/.env             — CLI/MCP/probe bank creds (server 也載,因 sync_runner 走 from_account)
#   3. (legacy) .env        — 過渡期保留 fallback
try:
    from dotenv import load_dotenv

    _ROOT = Path(__file__).resolve().parents[2]
    for _cand in (
        _ROOT / "backend" / "server" / ".env",
        _ROOT / "cli" / ".env",
        _ROOT / ".env",
    ):
        if _cand.exists():
            load_dotenv(_cand, override=False)
except ImportError:
    # dev env 沒裝 dotenv 就 skip（CI / production 由 secret manager 餵 env）
    pass

import time

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from backend.server.deps import current_user
from backend.server.security import APIKeyMiddleware

def _resolve_version() -> str:
    """Resolve app version: prefer installed package metadata, fallback to
    parsing pyproject.toml so editable / non-installed dev runs still report
    the real version instead of 0.0.0+dev."""
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version("thoth")
    except Exception:
        pass
    try:
        import tomllib
        from pathlib import Path
        # backend/server/app.py → repo root = parent.parent.parent
        root = Path(__file__).resolve().parent.parent.parent
        with (root / "pyproject.toml").open("rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        return "0.0.0+dev"

_APP_VERSION = _resolve_version()

# 2026-06-22: 設 backend.* logger 到 INFO + 接 uvicorn stdout handler.
# 預設 uvicorn 只把 uvicorn.access / uvicorn.error log 出來; app code 用
# logging.getLogger("backend.xxx").info(...) 全被吃掉 (root WARNING).
# 這次發現 push notification 完全靜默不見蹤跡, root cause 之一是 log invisibility.
# 不寫 basicConfig — uvicorn 已配 root handler, 改用 propagate 機制 + 強制 INFO level.
import logging
_uvicorn_logger = logging.getLogger("uvicorn")
_backend_logger = logging.getLogger("backend")
_backend_logger.setLevel(logging.INFO)
# uvicorn 已在 stderr 設 handler; 讓 backend.* 直接 propagate 到 root (root 有 handler)
# 萬一 root 沒 handler (test 環境), 補一個 stream handler 防 silent
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

# 2026-06-22 (L12, 0.3.17): APScheduler 啟動 / 停止 hook 進 FastAPI lifespan.
# - startup: 啟 BackgroundScheduler + 從 DB reload 所有 enabled schedule
# - shutdown: SIGTERM 時優雅停 (wait=False, 不等 in-flight sync job)
# 2026-06-28: FastAPI deprecated @app.on_event; 改 lifespan 消 warning.
def _scheduler_disabled() -> bool:
    return os.environ.get("THOTH_DISABLE_SCHEDULER", "").strip() in ("1", "true", "True")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not _scheduler_disabled():
        from backend.server import scheduler
        scheduler.start()
    try:
        yield
    finally:
        if not _scheduler_disabled():
            from backend.server import scheduler
            scheduler.shutdown(wait=False)


app = FastAPI(title="Bank Crawlers Server", version=_APP_VERSION, lifespan=lifespan)
_API_PREFIX = "/api" if os.environ.get("THOTH_STANDALONE", "").strip() in ("1", "true", "True") else ""

_request_logger = logging.getLogger("backend.request")
_request_logger.setLevel(logging.INFO)


@app.middleware("http")
async def _request_timing_middleware(request: Request, call_next):
    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        user_id = "-"
        auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            try:
                from backend.server.auth import decode_access_token
                claims = decode_access_token(auth.split(None, 1)[1])
                user_id = str(claims.get("sub", "-"))
            except Exception:
                user_id = "-"
        _request_logger.info(
            "request method=%s path=%s status=%s duration_ms=%.1f user_id=%s",
            request.method,
            request.url.path,
            status_code,
            duration_ms,
            user_id,
        )

# Phase C-Suggestion (2026-06-17): 嚴格模式 — server runtime 一律要求 BankStore 顯式傳 user_id
# 防 multi-tenant data leak (沒傳 → silent 寫 user_id=1 → 跨 user 看到別人資料)。
# CLI / tests / tools 不走這個 bootstrap → env 不設 → BankStore() default 1 保歷史單 user 語意。
os.environ.setdefault("THOTH_REQUIRE_EXPLICIT_USER_ID", "1")

# L8.5: server 級 X-API-Key（設 SERVER_API_KEY env 才啟用）
app.add_middleware(APIKeyMiddleware)

# CORS：frontend (Expo web localhost:8081 / iOS app localhost:8081 / Tauri desktop)
# 都跟 backend 是 cross-origin。env CORS_ORIGINS 用 comma-separated 設多個，
# 或留空走「dev default：localhost 任 port」(production 必設明確 origins)。
_cors_env = os.environ.get("CORS_ORIGINS", "").strip()
# dev default 為空：local Expo / iOS Simulator / Tauri 各種 port 由下方 cae default 補
_cors_origins = (
    [o.strip() for o in _cors_env.split(",") if o.strip()] if _cors_env else []
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=(
        None
        if _cors_env
        # dev: localhost / 127.0.0.1 / RFC1918 私有網段 / *.local
        # 涵蓋: 桌機 web、手機同 WiFi 開 Mac IP 的 web (192.168.x:8081 → 192.168.x:8000)
        else (
            r"^https?://("
            r"localhost|127\.0\.0\.1"
            r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
            r"|192\.168\.\d{1,3}\.\d{1,3}"
            r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
            r"|[\w-]+\.local"
            r")(:\d+)?$"
        )
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# auth dependency moved to backend.server.deps to avoid router import cycle.


@app.get(f"{_API_PREFIX}/healthz")
def healthz() -> dict:
    return {"status": "ok", "version": _APP_VERSION}


@app.get(f"{_API_PREFIX}/auth/me")
def me(user: dict = Depends(current_user)) -> dict:
    return {"id": user["id"], "email": user["email"], "created_at": user["created_at"]}


from backend.server.routers.accounts import router as accounts_router
from backend.server.routers.auth import router as auth_router
from backend.server.routers.auto_debit import router as auto_debit_router
from backend.server.routers.cache import router as cache_router
from backend.server.routers.cards import router as cards_router
from backend.server.routers.credentials import router as creds_router
from backend.server.routers.fx import router as fx_router
from backend.server.routers.portfolio import router as portfolio_router
from backend.server.routers.preferences import router as preferences_router
from backend.server.routers.push import router as push_router
from backend.server.routers.rules import router as rules_router
from backend.server.routers.sync import router as sync_router
from backend.server.routers.sync_preference import router as sync_preference_router
from backend.server.routers.sync_ws import router as sync_ws_router
from backend.server.routers.transactions import router as transactions_router

for _r in (
    auth_router, creds_router, sync_router, sync_preference_router, sync_ws_router,
    fx_router, rules_router,
    accounts_router, cache_router, transactions_router,
    # auto_debit 必須在 cards 之前 — /cards/auto-debit/* 是 specific path，
    # 否則 cards_router 的 /cards/{bank}/{card_no} 會 greedy match 變 404.
    auto_debit_router, cards_router, preferences_router,
    portfolio_router, push_router,
):
    app.include_router(_r, prefix=_API_PREFIX)


_frontend_dist_raw = os.environ.get("THOTH_FRONTEND_DIST", "").strip()
_frontend_dist = Path(_frontend_dist_raw).resolve() if _frontend_dist_raw else None
if _frontend_dist and _frontend_dist.is_dir():
    from fastapi.responses import FileResponse

    frontend_dist = _frontend_dist

    @app.get("/", include_in_schema=False)
    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str = ""):
        relative = Path(path or "index.html")
        candidates = [relative]
        if not relative.suffix:
            candidates.extend((relative.with_suffix(".html"), relative / "index.html"))
        for candidate in candidates:
            resolved = (frontend_dist / candidate).resolve()
            if resolved.is_relative_to(frontend_dist) and resolved.is_file():
                return FileResponse(resolved)
        return FileResponse(frontend_dist / "index.html")

