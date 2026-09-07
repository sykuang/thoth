"""Offline smoke for the built API image (stdlib + production deps, no pytest).

Run in a fresh container, never exec into a live service:
  docker run --rm -i --network none --entrypoint /app/.venv/bin/python IMAGE - < deploy/smoke_api_image.py
Or mount the checkout in an ACR multi-step task and run:
  cd /app && /app/.venv/bin/python - < /workspace/deploy/smoke_api_image.py
No env file/credentials needed. This file is not copied into the runtime image.
Checks imports, normal lifespan and SQLite-backed routes; never enqueues jobs,
starts browsers, logs into banks or sends push messages. Not a PostgreSQL test.
"""
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    # Ignore inherited production configuration, including dotenv fallback.
    os.environ.clear()
    os.environ.update(
        DB_BACKEND="sqlite", SYNC_EXECUTION_MODE="external",
        PYTHON_DOTENV_DISABLED="1", JWT_SECRET="offline-smoke-only-not-a-production-secret",
        REGISTER_DELAY_SECONDS="0",
    )
    network_attempts = []

    def forbid_network(event, _args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            network_attempts.append(event)
            raise AssertionError("offline smoke attempted network access")

    sys.addaudithook(forbid_network)
    with tempfile.TemporaryDirectory(prefix="thoth-api-smoke-") as data:
        os.environ["BANK_DATA_ROOT"] = data
        from cryptography.fernet import Fernet
        os.environ["SERVER_FERNET_KEY"] = Fernet.generate_key().decode()

        import ddddocr  # noqa: F401 — also validate retained native OCR wheel libraries
        import migrations.backfill_ctbc_post_date  # noqa: F401
        from fastapi.testclient import TestClient
        from backend.server.app import app
        from backend.server import scheduler, sync_runner
        from backend.server.push import registry

        for bank in sorted(sync_runner.SUPPORTED_BANKS):
            assert isinstance(sync_runner._required_history_domains(bank), frozenset), bank
        for provider, expected in (("none", "NoOpNotifier"), ("expo", "ExpoPushProvider"), ("webhook", "WebhookNotifier")):
            os.environ["PUSH_PROVIDER"] = provider
            assert type(registry.get_notifier()).__name__ == expected
        os.environ["PUSH_PROVIDER"] = "none"

        assert not scheduler.in_process_enabled()
        with TestClient(app) as client:
            assert scheduler._scheduler is None
            assert client.get("/healthz").json()["status"] == "ok"
            assert client.get("/auth/me").status_code == 401
            registered = client.post("/auth/register", json={
                "email": "offline-smoke@palace.example", "password": "SyntheticTestPassword02!",
            })
            assert registered.status_code == 201, registered.status_code
            client.headers["Authorization"] = f"Bearer {registered.json()['access_token']}"
            assert client.get("/auth/me").json()["id"] == registered.json()["user_id"]
            for route in ("/accounts", "/cards", "/sync/jobs"):
                response = client.get(route)
                assert response.status_code == 200, (route, response.status_code)
                assert response.json() == [], route
        assert scheduler._scheduler is None
        assert (Path(data) / "server.sqlite").is_file()
        assert not network_attempts, network_attempts
    print(f"API image offline smoke OK: {len(sync_runner.SUPPORTED_BANKS)} crawler capabilities, 3 push providers, lifecycle/routes")


if __name__ == "__main__":
    main()
