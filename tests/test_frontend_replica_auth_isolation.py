"""Frontend local replica must be erased across authenticated owner transitions."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTH = ROOT / "frontend/src/stores/auth.ts"
WEB_STORE = ROOT / "frontend/src/lib/replicaStore.web.ts"


def test_auth_store_clears_replica_before_logout_or_account_switch() -> None:
    source = AUTH.read_text()

    assert "activateReplicaOwner, clearReplicaOwner, makeReplicaOwnerKey" in source
    assert "import { replicaStore } from '@/lib/replicaStore';" in source
    assert "void clearReplicaOwner(replicaStore, state.serverUrl, state.email);" in source
    assert "state.email && state.email !== email" in source
    assert "activateReplicaOwner(makeReplicaOwnerKey(state.serverUrl, email));" in source
    assert "logout: () => set((state) => {" in source
    logout = source[source.index("logout: () => set"):source.index("_setHydrated:", source.index("logout: () => set"))]
    assert logout.index("clearReplicaOwner") < logout.index("return { token: null")


def test_web_replica_removes_the_legacy_cross_user_snapshot_key() -> None:
    source = WEB_STORE.read_text()
    assert "localStorage.removeItem('thoth.frontendDataset.v1')" in source
