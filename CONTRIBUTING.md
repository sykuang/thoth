# Contributing to Thoth

Thanks for your interest in contributing! This document covers the dev setup,
code conventions, and PR process.

## Code of Conduct

Be kind, be respectful. We're all just trying to build cool things.

If you witness or experience behaviour that violates this, contact the maintainers
via a GitHub issue (you can request the issue be made private).

## Development Setup

### Backend

```bash
git clone https://github.com/YOUR-USERNAME/thoth.git
cd thoth
uv sync                         # or: pip install -e .
.venv/bin/playwright install chromium
cp .env.example .env            # fill in JWT_SECRET + SERVER_FERNET_KEY
.venv/bin/uvicorn backend.server.app:app --reload --port 8000
```

### Frontend (web dev mode)

```bash
cd frontend
pnpm install
EXPO_PUBLIC_API_URL=http://localhost:8000 pnpm exec expo start --web --port 8081
```

### Run tests before pushing

```bash
# Backend (~200 tests, 40s) — using uv
uv run pytest

# Backend lint (informational for now, will become strict)
uv run ruff check backend/ cli/ tests/

# Frontend typecheck
cd frontend && pnpm typecheck

# Frontend lint (currently informational)
cd frontend && pnpm lint
```

### Continuous Integration

Every push to `main` and every PR triggers `.github/workflows/ci.yml`:

| Job | Step | Blocks merge? |
|---|---|---|
| backend | `uv run ruff check` | No (informational, will become strict) |
| backend | `uv run pytest` | **Yes** |
| frontend | `pnpm typecheck` | **Yes** |
| frontend | `pnpm lint` | No (informational) |

The summary job `ci-summary` is the single required status check for branch
protection — it depends on `backend` and `frontend` passing.

Note: Playwright E2E tests (`tests/test_e2e_playwright_web.py`) are excluded
from CI because they need a running browser + frontend. Run them locally with
`uv run pytest tests/test_e2e_playwright_web.py` if you touch the login flow.

## Adding a New Bank

If you want to add support for a Taiwan bank we don't cover yet:

1. **Create the crawler** under `backend/banks/<bank>.py` following the existing
   pattern. Subclass `BankCrawler` (`backend/core/base.py`). Implement:
   - `login(page)` — return True on success, raise on failure
   - `collect(page, collector) -> dict` — return raw scrape result
2. **Create the credentials class** under `backend/core/creds.py` —
   subclass `BankCreds`, declare fields, register in `ALL_CREDS`.
3. **Create the persist function** in `backend/core/persist.py` —
   transform raw collect dict → store API calls (`upsert_account`,
   `upsert_card`, `refresh_card_pending`, etc.).
4. **Register in dispatcher** — add to `backend/server/sync_runner.py`
   `SUPPORTED_BANKS` and `routers/rules.py` whitelist.
5. **Write tests** under `tests/test_persist_<bank>.py` covering empty data,
   single account, multi-account, edge cases.
6. **Update frontend** `frontend/src/types/api.ts` — add bank to
   `SupportedBank` type + `BANK_LABELS`.

See existing banks (e.g. `backend/banks/sinopac.py`) as reference. Banks vary
wildly in technical complexity — some have clean APIs (sinopac, ctbc), some are
SPA hell (taishin, scsb), some require true mouse events for mega menus (taishin).

## Coding Conventions

### Python

- **Style**: black-compatible (4-space indent), no enforced line length
- **Type hints**: encouraged, especially for public functions
- **Comments**: Traditional Chinese OK, but module-level docstrings should have
  an English summary line at the top so international contributors can navigate
- **Logging**: use the bank's `_log(f"[{bank}][step] ...")` pattern
- **Tests**: pytest, fixture-based, no live network in default test run

### TypeScript

- **Style**: Prettier defaults, no enforced line length
- **Type strict**: `tsc --noEmit` must pass with zero errors
- **Components**: function components only, no class components
- **State**: zustand for global state, TanStack Query for server state
- **Styling**: NativeWind classNames, no inline styles unless necessary

### Commit Messages

Format: `<type>(<scope>): <subject>`

Examples:
- `feat(cathay): add credit card pending transactions`
- `fix(scb): handle 重複登入 modal correctly`
- `chore: bump expo to SDK 57`
- `docs: clarify Fernet key rotation policy`

### Avoid in commits

- Real user PII (account numbers, names, emails)
- Hardcoded secrets / API keys
- Personal commentary unrelated to the change
- Binary blobs over 100KB (use Git LFS if needed)

## Pull Request Process

1. **Fork** the repo, create a feature branch from `main`
2. **Make your changes**, write tests
3. **Run tests + typecheck** locally before pushing
4. **Open a PR** with:
   - Clear description of what changed and why
   - Screenshots for UI changes
   - Test results (pytest output, typecheck output)
   - Any breaking changes called out explicitly
5. **Be patient** — maintainer review may take a few days
6. **Iterate** based on review comments

## Things We Care About

### Security

- **Never commit real credentials.** Always use `.env` (git-ignored).
- **Never log raw passwords / tokens / cookies** — even temporarily.
- **Never bypass Fernet** — credentials in DB must be encrypted, no plain-text
  fallback paths.
- **Always validate user input** — Pydantic models, not raw `dict`.
- **Be careful with `eval()`, `exec()`, and `os.system()`** — basically don't use them.

### Privacy

- **Mask sensitive data at display layer** — credit card numbers, account numbers,
  national IDs are always masked in UI.
- **Don't store more than needed** — e.g. national ID is needed for login but
  shouldn't be in `daily_metrics`. See existing `persist_*` functions for the pattern.

### Reliability

- **Don't retry bank logins automatically.** Taiwan banks lock accounts after
  3-5 failed attempts. The codebase enforces `max_attempts=1` for a reason.
- **Handle "duplicate login" modals as a feature, not a retry.** Banks like
  Taishin and SCB explicitly need a 2-step login flow that looks like a retry
  but isn't. See `backend/banks/taishin.py` for the pattern.
- **Don't bypass rate limits.** Pace your syncs. One full sync per day is plenty.

## Questions?

Open a [Discussion](https://github.com/YOUR-USERNAME/thoth/discussions)
on GitHub. For bugs, file an [Issue](https://github.com/YOUR-USERNAME/thoth/issues).
