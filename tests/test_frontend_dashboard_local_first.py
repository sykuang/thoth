"""Regression contract for Dashboard local-first cold-start rendering."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "frontend/src/app/(tabs)/dashboard.tsx"


def test_dashboard_hydrates_primary_cards_from_one_complete_snapshot() -> None:
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "const datasetQ = useFrontendDatasetCache();" in source
    assert "const localDashboard = datasetQ.data?.dashboardCache;" in source
    assert "remoteDashboard?.ownerKey === ownerKey" in source
    assert "const accounts = dashboard?.accounts ?? [];" in source
    assert "const portfolio = dashboard?.portfolio;" in source
    assert "const stats = dashboard?.stats;" in source
    assert "accountsQ.data ?? localDashboard" not in source
    assert "portfolioQ.data ?? localDashboard" not in source
    assert "statsQ.data ?? localDashboard" not in source
    assert "'/auth/me'" not in source


def test_dashboard_remote_queries_and_persistence_are_owner_bound() -> None:
    source = DASHBOARD.read_text(encoding="utf-8")

    for query_key in (
        "['accounts', ownerKey]",
        "['portfolio', 'summary', ownerKey]",
        "['transactions', 'stats', ownerKey]",
        "['sync', 'jobs', ownerKey]",
        "['auto-debit', 'reminders', ownerKey]",
    ):
        assert query_key in source

    assert "fetchCompleteDashboardCache" in source
    assert "activeOwnerRef.current !== ownerKey" in source
    assert "cachedAt: new Date().toISOString()" not in source


def test_query_invalidation_refreshes_and_persists_a_complete_snapshot() -> None:
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "dataUpdateCount" in source
    for revision_signal in (
        "accountsQ.data",
        "portfolioQ.data",
        "statsQ.data",
    ):
        assert revision_signal in source
    assert "void refreshDashboard();" in source
    assert "persistDashboardCache(cache)" in source
    assert "throwOnError: true" in source
    assert "if (!completed) return;" in source
    assert "staleTime: 1000" not in source


def test_cached_empty_dashboard_does_not_wait_for_remote_accounts() -> None:
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "Boolean(dashboard) && ready.length === 0" in source
    assert "!datasetQ.isFetched && !datasetQ.isError" in source
    assert "accountsQ.isPending" in source
    assert "暫時無法載入財務摘要" in source
