# SnapTrade MVP Implementation Plan

> **For Hermes:** Implement with strict TDD and review the exact final diff.

**Goal:** 在 Thoth 增加 SnapTrade 券商連結、帳戶／現金／持倉／交易活動同步與前端投資頁。

**Architecture:** 新增一個深模組 `backend/server/snaptrade.py`，把 SDK、userSecret 加密、全批次 fail-closed 同步與本地 snapshot persistence 藏在 `status/connect/sync/snapshot` 小介面後。Router 只做 JWT ownership 與 HTTP mapping；frontend 新增投資 tab，開啟 SnapTrade Connection Portal 並顯示本地快照。既有銀行 schema/portfolio summary 不改，避免把市場估值混進銀行現金口徑。

**Tech Stack:** FastAPI、portable SQLite/PostgreSQL DDL、Fernet、`snaptrade-python-sdk==11.0.182`、Expo Router、React Query。

## 安全鐵令

- userSecret 只以 `SERVER_FERNET_KEY` 加密存 server DB，API/log 永不回傳。
- `SNAPTRADE_CLIENT_ID` / `SNAPTRADE_CONSUMER_KEY` 只由 server env 注入，frontend 永不接觸。
- 所有 brokerage rows 必含 `user_id`；讀寫一律由 JWT user scope。
- accounts/balances/positions 全批次抓取成功後才 transaction replace；partial/exception 保留舊 snapshot。
- position quantity 用 `units`，只有 `units` 缺失才 fallback `fractional_units`，禁止相加。
- activities 只對已知有真實 Transactions 支援的 brokerage slug抓取；不支援者明示，不合成交易。
- 本次不觸發真實券商連線或交易；SnapTrade 僅使用 read-only Connection Portal。

## Task 1 — Schema + encrypted identity

- Modify `backend/server/db.py`: `snaptrade_users`, `brokerage_accounts`, `brokerage_balances`, `brokerage_positions`, `brokerage_activities`; indexes and unique user-scoped keys.
- Test `tests/test_snaptrade_routes.py`: two users cannot see each other; status never returns secret.
- RED → minimal repo implementation → GREEN.

## Task 2 — Gateway + fail-closed sync

- Create `backend/server/snaptrade.py` with SDK adapter and `SnapTradeService`.
- Test mocked upstream: register/connect, dedupe, units semantics, cash/positions/activity normalization, partial fetch rollback, unsupported activity slug.
- Pin SDK 11.0.182 and typing-extensions floor in `pyproject.toml`/`uv.lock`.

## Task 3 — Authenticated routes

- Create `backend/server/routers/snaptrade.py`:
  - `GET /snaptrade/status`
  - `POST /snaptrade/connect`
  - `POST /snaptrade/sync`
  - `GET /snaptrade/portfolio`
- Modify `backend/server/app.py` and `tests/conftest.py` wiring.
- Verify unauthenticated 401 and per-user ownership.

## Task 4 — Frontend investment surface

- Modify `frontend/src/types/api.ts` with SnapTrade snapshot types.
- Create `frontend/src/app/(tabs)/investments.tsx`: status, connect, refresh, accounts/cash/positions/activities.
- Modify `frontend/src/app/(tabs)/_layout.tsx` to add 投資 tab.
- Use existing `api()` and React Query; external portal via `Linking.openURL`.
- Verify `pnpm typecheck` and Expo web export.

## Task 5 — Ship

- Targeted pytest, full pytest, Ruff, lock check, frontend typecheck/export, secret/diff audit.
- Independent current-diff review; fix blockers.
- Bump version, path-scoped commit, push, require CI green.
- Add Azure Key Vault/ACA env references for SnapTrade keys without printing values; deploy immutable image; verify latest ready revision + `/healthz`.
- No authenticated SnapTrade live sync until Connection Portal is completed by the user.

## Definition of Done

1. `pytest tests/test_snaptrade_routes.py -q` passes.
2. Full pytest + Ruff + `uv lock --check` + `git diff --check` pass.
3. `pnpm typecheck` and `pnpm web:export` pass.
4. `GET /snaptrade/status` and `/portfolio` reject missing JWT and isolate users.
5. Production health reports the new version; frontend contains 投資 tab.
6. Runtime status may be `missing_configuration` until Azure SnapTrade keys are injected; do not call that a connected brokerage.
