"""Owner-bound auth wiring; executable interleavings live in frontend api.test.cjs."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_owner_bound_api_refreshes_with_lifecycle_and_credential_guards() -> None:
    api = (ROOT / "frontend/src/lib/api.ts").read_text(encoding="utf-8")
    owner_hook = (ROOT / "frontend/src/hooks/useOwnerBoundApi.ts").read_text(encoding="utf-8")
    auth = (ROOT / "frontend/src/stores/auth.ts").read_text(encoding="utf-8")
    credentials = (ROOT / "frontend/src/lib/credentials.ts").read_text(encoding="utf-8")

    session = api[api.index("function currentAuthSessionKey"):api.index("function currentAuthCredentialsKey")]
    assert "sessionEpoch" in session
    assert "refreshToken" not in session
    assert "sessionEpoch: state.sessionEpoch + 1" in auth
    partialize = auth[auth.index("partialize:"):auth.index("onRehydrateStorage:")]
    assert "sessionEpoch" not in partialize

    assert "const refreshGate = new SessionPromiseGate<string>();" in api
    assert "const biometricReLoginGate = new SessionPromiseGate<string>();" in api
    assert "authRetryKey ??" not in api
    recovery = api[api.index("function captureAuthRecovery"):api.index("async function getOrStartRefresh")]
    assert "currentAuthSessionKey() !== sessionKey" in recovery
    assert "currentAuthCredentialsKey() !== credentialsKey" in recovery
    assert "authRetryGuard?.();" in recovery

    biometric = api[api.index("async function getOrStartBiometricReLogin"):api.index("async function warmUpSync")]
    assert biometric.index("storedCredentialsMatchSession(creds") < biometric.index("form.append('password'")
    assert "captureAuthRecovery(authRetryGuard)" in biometric
    assert ".setTokens(data.access_token" in biometric
    assert ".setAuth(" not in biometric
    assert "JSON.stringify({ serverUrl, email, password }" in credentials

    assert "const requestAuthSessionKey = init.skipAuth ? null : currentAuthSessionKey();" in api
    assert "const text = await res.text();\n  if (!init.skipAuth) assertRequestAuthSession();" in api
    assert "skipAuthRetry: true" not in owner_hook
    assert "authRetryGuard: () => assertReplicaOwnerEpoch(ownerKey, ownerEpoch)" in owner_hook
