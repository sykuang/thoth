"""Owner-bound requests may refresh only while their replica owner epoch stays active."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "frontend/src/lib/api.ts"
OWNER_HOOK = ROOT / "frontend/src/hooks/useOwnerBoundApi.ts"
CREDENTIALS = ROOT / "frontend/src/lib/credentials.ts"


def test_owner_bound_api_refreshes_with_epoch_guard_instead_of_failing_401() -> None:
    api = API.read_text(encoding="utf-8")
    owner_hook = OWNER_HOOK.read_text(encoding="utf-8")
    credentials = CREDENTIALS.read_text(encoding="utf-8")

    assert "authRetryGuard?: () => void;" in api
    assert "authRetryKey?: string;" in api
    assert "const refreshGate = new SessionPromiseGate<string>();" in api
    assert "function currentAuthSessionKey(): string" in api
    refresh = api[api.index("async function getOrStartRefresh"):api.index("async function safeReadJson")]
    assert "const authSessionKeyAtStart = currentAuthSessionKey();" in refresh
    assert "if (currentAuthSessionKey() !== authSessionKeyAtStart)" in refresh
    assert refresh.count("authRetryGuard?.();") >= 2
    assert refresh.index("authRetryGuard?.();", refresh.index("resp.json")) < refresh.index("setTokens(")

    biometric = api[api.index("async function getOrStartBiometricReLogin"):api.index("export async function api")]
    assert biometric.index("storedCredentialsMatchSession(creds") < biometric.index("form.append('password'")
    assert biometric.index("currentAuthSessionKey() !== authSessionKeyAtStart") < biometric.index("form.append('password'")
    assert "active.serverUrl.replace(/\\/+$/, '')" in biometric
    assert "serverUrl: string;" in credentials
    assert "JSON.stringify({ serverUrl, email, password }" in credentials

    retry = api[api.index("if (res.status === 401 && !init.skipAuth)"):api.index("// 204 No Content")]
    assert "await getOrStartRefresh(init.authRetryKey, assertRequestAuthSession);" in retry
    assert "assertRequestAuthSession();" in retry
    assert retry.index("if (init._retriedAfterBiometric)") < retry.index("if (init._retriedAfterRefresh)")

    request = api[api.index("export async function api"):api.index("// L9: 401")]
    assert "let requestAuthSessionKey = init.skipAuth ? null : currentAuthSessionKey();" in request
    assert "auth session changed during request" in request
    assert "if (!init.skipAuth) assertRequestAuthSession();" in request
    assert "const text = await res.text();\n  if (!init.skipAuth) assertRequestAuthSession();" in api

    assert "skipAuthRetry: true" not in owner_hook
    assert "authRetryKey: `${ownerKey}:${ownerEpoch}`" in owner_hook
    assert "authRetryGuard: () => assertReplicaOwnerEpoch(ownerKey, ownerEpoch)" in owner_hook
