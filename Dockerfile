# Phase 0 spike: thoth backend deployment to Azure Container Apps
#
# Base: Microsoft's official Playwright Python image (Chromium + all system
# deps preinstalled). Tracks Playwright version to whatever's bundled with
# scrapling[fetchers]. Currently noble (Ubuntu 24.04) on Python 3.12.
#
# Phase 9 update (2026-06-15): bump 1.49 → 1.60.0 because scrapling/patchright
# advanced. MCR currently has up to v1.60.0-noble (v1.60.1 doesn't exist).
# patchright bundles its own chromium-1223 binary so even with playwright 1.60.0
# base, we re-install patchright's chromium below to match its expected layout.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble
# Design decisions captured in wiki/concepts/thoth-backend-deployment-survey.md

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install uv for fast dep resolve (matches local dev workflow)
RUN pip install --no-cache-dir uv==0.5.11

# Copy dep manifests first for Docker layer cache
COPY pyproject.toml uv.lock README.md ./

# Install dependencies. --frozen reuses uv.lock exactly so prod matches dev.
# --no-dev skips pytest/ruff/vulture (saves ~150MB on prod image).
RUN uv sync --frozen --no-dev --no-install-project

# Copy source (after deps so dep layer cached). cli/ also copied because some
# backend modules import from cli.creds even in server mode (legacy).
COPY backend/ ./backend/
COPY cli/ ./cli/
COPY migrations/ ./migrations/

# Install thoth package itself (now that source is in place)
RUN uv sync --frozen --no-dev

# Phase 9: patchright 1.60.1 expects /ms-playwright/chromium-1223/...
# but the base image's chromium-1222 was installed for playwright 1.60.0.
# Re-install patchright's bundled Chromium and branded Chrome. Rakuten opts into
# channel="chrome" because Incapsula rejects the bundled Chromium fingerprint.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN uv run patchright install chromium chrome --with-deps 2>&1 | tail -20

# Verify both the default Chromium and Rakuten's real-Chrome channel are usable.
RUN uv run python -c "from scrapling import StealthyFetcher; print('scrapling OK:', StealthyFetcher)"
RUN uv run python -c "from scrapling.engines._browsers._stealth import StealthySession; s=StealthySession(real_chrome=True, headless=True); s.start(); p=s.context.new_page(); print('scrapling real Chrome OK:', p.evaluate('navigator.userAgent')); p.close(); s.close()"

# 2026-06-22 (0.3.16 incident hardening): verify push providers actually import
# in this --no-dev prod image. Catches "httpx in optional-deps" / "expo.py
# import at module top" class of bugs at build time, not at first runtime sync.
# Don't use pytest (dev-only); inline import + instantiate covers the same.
# Single-line python -c (ACR scanner 不懂 multi-line backslash + python `from X import Y`,
# 會把 import 行當 Dockerfile keyword 試 parse 然後爆 "unable to understand line").
RUN uv run python -c "import os; from backend.server.push.providers.none import NoOpNotifier; from backend.server.push.providers.webhook import WebhookNotifier; from backend.server.push.providers.expo import ExpoPushProvider; from backend.server.push import registry; assert NoOpNotifier(); assert WebhookNotifier(); assert ExpoPushProvider(); os.environ['PUSH_PROVIDER']='expo'; registry._NOTIFIER_CACHE.clear(); n = registry.get_notifier(); assert n.__class__.__name__ == 'ExpoPushProvider', f'expected ExpoPushProvider got {n}'; print('push providers OK:', n.__class__.__name__)"

# Ensure pwuser owns everything before switching user. Without this the
# uvicorn process can't read its own modules at runtime
# (PermissionError: [Errno 13] Permission denied: '/app/backend/__init__.py').
RUN chown -R pwuser:pwuser /app

# Container Apps will set INGRESS to 8000. Document for humans.
EXPOSE 8000

# Run as the non-root playwright user that the base image set up.
USER pwuser

# uvicorn directly — Container Apps handles process supervision via the
# container restart policy. No need for gunicorn/supervisord.
CMD ["uv", "run", "uvicorn", "backend.server.app:app", \
     "--host", "0.0.0.0", "--port", "8000"]
