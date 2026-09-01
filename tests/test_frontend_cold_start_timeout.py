from pathlib import Path


def test_frontend_allows_azure_scale_to_zero_cold_start() -> None:
    api_source = Path("frontend/src/lib/api.ts").read_text()
    login_source = Path("frontend/src/app/login.tsx").read_text()

    assert "const DEFAULT_TIMEOUT_MS = 90_000;" in api_source
    assert "const CONNECTION_TEST_TIMEOUT_MS = 90_000;" in login_source
    assert "setTimeout(() => ctrl.abort(), CONNECTION_TEST_TIMEOUT_MS)" in login_source
