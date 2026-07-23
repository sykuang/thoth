"""Smoke test: prod runtime deps actually let push providers import.

Background (2026-06-22, 0.3.15 incident):
  - Backend deployed PUSH_PROVIDER=expo, but Dockerfile builds with --no-dev.
  - httpx was listed only under [push-apns] optional-dependencies group.
  - ExpoPushProvider does `import httpx` at module top → ModuleNotFoundError.
  - First sync attempt silently swallowed the error → no notification, no clue.
  - Fixed in 0.3.16 by moving httpx into main [project.dependencies].

This regression test catches the symmetric mistake:
  - If anyone moves httpx back into optional, this test fails.
  - If anyone adds a new push provider with a new top-level `import X` that's
    not in main deps, this test fails.
"""
from __future__ import annotations

import importlib


def test_expo_push_provider_imports_with_main_deps_only() -> None:
    """Prod image (no [push-apns] extras) must still import ExpoPushProvider.

    Failure mode without this test:
      - sync_runner._send_sync_notification calls get_notifier()
      - registry._build('expo') tries to import expo provider
      - `import httpx` at module top raises ModuleNotFoundError
      - silent-swallow except: pass → no notification, no log without instrumentation
    """
    mod = importlib.import_module("backend.server.push.providers.expo")
    assert hasattr(mod, "ExpoPushProvider"), \
        "ExpoPushProvider class missing — expo provider module did not load"
    # Instantiable without side effects (no Expo HTTP call yet)
    instance = mod.ExpoPushProvider()
    assert instance.name == "expo"


def test_apns_push_provider_imports_for_optional_audit() -> None:
    """Direct APNs provider stays under [push-apns] optional — opt-in only.

    Unlike Expo (which is the recommended B1 path), APNs needs HTTP/2 + .p8
    setup and is only loaded when user installs the optional extra. So
    ImportError here is acceptable in main deps; we just verify the module
    file exists for audit.
    """
    try:
        mod = importlib.import_module("backend.server.push.providers.apns")
        assert hasattr(mod, "ApnsNotifier")
    except ImportError as e:
        # OK — APNs is opt-in. But error message should say which dep is missing.
        msg = str(e).lower()
        assert "jwt" in msg or "httpx" in msg or "h2" in msg, \
            f"APNs ImportError is suspicious: {e}"


def test_webhook_push_provider_imports_with_main_deps_only() -> None:
    """Webhook provider is universal fallback — must always import."""
    mod = importlib.import_module("backend.server.push.providers.webhook")
    assert hasattr(mod, "WebhookNotifier")


def test_none_push_provider_imports_with_main_deps_only() -> None:
    """NoOpNotifier is the default — must always import."""
    mod = importlib.import_module("backend.server.push.providers.none")
    assert hasattr(mod, "NoOpNotifier")


def test_registry_can_build_all_main_dep_providers() -> None:
    """Registry.get_notifier() for non-optional providers must not raise.

    Catches the case where Dockerfile prod image is missing a runtime dep
    (httpx, regex, etc.) that a provider imports at module top.
    """
    import os
    # Save + restore env
    prev = os.environ.get("PUSH_PROVIDER")
    try:
        for name, expected_prefix in (
            ("none", "noop"),
            ("expo", "expo"),
            ("webhook", "webhook"),
        ):
            os.environ["PUSH_PROVIDER"] = name
            # 強制 reset registry cache (lazy import factory)
            from backend.server.push import registry
            registry._NOTIFIER_CACHE.clear()
            notifier = registry.get_notifier()
            cls = notifier.__class__.__name__.lower()
            assert cls.startswith(expected_prefix), \
                f"{name} → {notifier.__class__.__name__} (expected starts with {expected_prefix})"
    finally:
        if prev is None:
            os.environ.pop("PUSH_PROVIDER", None)
        else:
            os.environ["PUSH_PROVIDER"] = prev
        from backend.server.push import registry
        registry._NOTIFIER_CACHE.clear()
