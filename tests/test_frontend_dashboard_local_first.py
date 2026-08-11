"""Regression contract for the Dashboard replica-only read path."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "frontend/src/app/(tabs)/dashboard.tsx"
DATASET_HOOK = ROOT / "frontend/src/hooks/useFrontendDatasetCache.ts"
OWNER_HOOK = ROOT / "frontend/src/hooks/useOwnerBoundApi.ts"


def test_dashboard_renders_one_replica_derived_snapshot() -> None:
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "const datasetQ = useFrontendDatasetCache();" in source
    assert "const dashboard = datasetQ.data?.dashboardCache;" in source
    assert "const accounts = dashboard?.accounts ?? [];" in source
    assert "const portfolio = dashboard?.portfolio;" in source
    assert "const stats = dashboard?.stats;" in source
    assert "remoteDashboard" not in source
    assert "localDashboard" not in source


def test_dashboard_does_not_read_or_persist_server_aggregates() -> None:
    source = DASHBOARD.read_text(encoding="utf-8")

    for forbidden in (
        "'/accounts'",
        "'/portfolio/summary'",
        "'/transactions/stats'",
        "fetchCompleteDashboardCache",
        "hasNewerDashboardRevision",
        "persistDashboardCache",
        "dataUpdateCount",
    ):
        assert forbidden not in source


def test_dashboard_keeps_only_live_owner_bound_reads() -> None:
    source = DASHBOARD.read_text(encoding="utf-8")

    for query_key in (
        "['sync', 'jobs', ownerKey, ownerEpoch]",
        "['auto-debit', 'reminders', ownerKey, ownerEpoch]",
    ):
        assert query_key in source
    owner_hook = OWNER_HOOK.read_text(encoding="utf-8")
    assert "guardReplicaOwnerRequest" in owner_hook
    assert "authRetryGuard: () => assertReplicaOwnerEpoch(ownerKey, ownerEpoch)" in owner_hook


def test_sync_completion_refreshes_only_canonical_replica_and_live_reminders() -> None:
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "qc.invalidateQueries({ queryKey: ['frontend-dataset'] });" in source
    assert "qc.invalidateQueries({ queryKey: ['auto-debit', 'reminders'] });" in source
    assert "queryKey: ['portfolio']" not in source
    assert "queryKey: ['transactions']" not in source
    assert "queryKey: ['accounts']" not in source


def test_dashboard_reprojects_at_utc_month_rollover() -> None:
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "dashboardMonthRef" in source
    assert "setInterval" in source
    assert "new Date().toISOString().slice(0, 7)" in source
    assert "qc.invalidateQueries({ queryKey: ['frontend-dataset'] });" in source
    assert "clearInterval" in source


def test_corrupt_current_replica_is_discarded_before_bootstrap_retry() -> None:
    source = DATASET_HOOK.read_text(encoding="utf-8")

    assert "if (!dataset.dashboardCache)" in source
    assert "await discardReplica(replicaStore, ownerKey, ownerEpoch);" in source
    assert source.count("await syncReplica(replicaStore, ownerKey, requestReplica)") >= 2
    assert "Replica bootstrap did not produce a complete Dashboard" in source


def test_incomplete_initial_bootstrap_is_not_bootstrapped_twice() -> None:
    source = DATASET_HOOK.read_text(encoding="utf-8")

    assert "const firstSyncWasPull" in source
    assert "if (!firstSyncWasPull)" in source


def test_missing_local_projection_is_loading_or_error_not_zero_truth() -> None:
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "const dashboardLoading = !dashboard && (!datasetQ.isFetched || datasetQ.isRefetching);" in source
    assert "const dashboardError = !dashboard && datasetQ.isFetched && !datasetQ.isRefetching;" in source
    assert "Boolean(dashboard) && ready.length === 0" in source
    assert "暫時無法載入財務摘要" in source
