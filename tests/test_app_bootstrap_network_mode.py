import asyncio
import os
import subprocess
import sys

from fastapi.testclient import TestClient


def test_network_bootstrap_skips_database_migration(monkeypatch) -> None:
    from backend.core import store
    from backend.server import app as app_module

    monkeypatch.setenv("THOTH_BOOTSTRAP_NETWORK_ONLY", "1")
    monkeypatch.setattr(
        store,
        "migrate_existing_bank_stores",
        lambda _banks: (_ for _ in ()).throw(AssertionError("database migration ran")),
    )

    async def exercise() -> None:
        async with app_module.lifespan(app_module.app):
            pass

    asyncio.run(exercise())


def test_in_process_mode_starts_and_stops_scheduler(monkeypatch) -> None:
    from backend.core import store
    from backend.server import app as app_module
    from backend.server import scheduler

    monkeypatch.delenv("THOTH_BOOTSTRAP_NETWORK_ONLY", raising=False)
    monkeypatch.delenv("THOTH_DISABLE_SCHEDULER", raising=False)
    monkeypatch.setenv("SYNC_EXECUTION_MODE", "inprocess")
    monkeypatch.setattr(store, "migrate_existing_bank_stores", lambda _banks: None)
    calls: list[str] = []
    monkeypatch.setattr(scheduler, "start", lambda: calls.append("start"))
    monkeypatch.setattr(scheduler, "shutdown", lambda **_kwargs: calls.append("shutdown"))

    async def exercise() -> None:
        async with app_module.lifespan(app_module.app):
            assert calls == ["start"]

    asyncio.run(exercise())
    assert calls == ["start", "shutdown"]


def test_external_mode_does_not_start_scheduler(monkeypatch) -> None:
    from backend.core import store
    from backend.server import app as app_module
    from backend.server import scheduler

    monkeypatch.delenv("THOTH_BOOTSTRAP_NETWORK_ONLY", raising=False)
    monkeypatch.delenv("THOTH_DISABLE_SCHEDULER", raising=False)
    monkeypatch.setenv("SYNC_EXECUTION_MODE", "external")
    monkeypatch.setattr(store, "migrate_existing_bank_stores", lambda _banks: None)
    monkeypatch.setattr(
        scheduler,
        "start",
        lambda: (_ for _ in ()).throw(AssertionError("scheduler started")),
    )

    async def exercise() -> None:
        async with app_module.lifespan(app_module.app):
            pass

    asyncio.run(exercise())


def test_network_bootstrap_serves_only_health(monkeypatch) -> None:
    from backend.server import app as app_module

    monkeypatch.setenv("THOTH_BOOTSTRAP_NETWORK_ONLY", "1")
    with TestClient(app_module.app) as client:
        assert client.get("/healthz").status_code == 200
        blocked = client.get("/auth/me")

    assert blocked.status_code == 503
    assert blocked.json() == {"detail": "network bootstrap in progress"}


def test_network_bootstrap_does_not_register_websocket_route() -> None:
    environment = os.environ.copy()
    environment["THOTH_BOOTSTRAP_NETWORK_ONLY"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from backend.server.app import app; "
                "assert not any(getattr(r, 'path', '').startswith('/ws/') "
                "for r in app.routes)"
            ),
        ],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
