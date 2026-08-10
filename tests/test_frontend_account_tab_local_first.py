"""Regression contract for Accounts-tab local-first cold-start rendering."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_TAB = ROOT / "frontend/src/app/(tabs)/cards/index.tsx"
DATASET_HOOK = ROOT / "frontend/src/hooks/useFrontendDatasetCache.ts"
OWNER_HOOK = ROOT / "frontend/src/hooks/useOwnerBoundApi.ts"
SNAPTRADE = ROOT / "frontend/src/components/SnapTradeSections.tsx"
AUTO_DEBIT = ROOT / "frontend/src/components/AutoDebitSettingModal.tsx"


def test_accounts_tab_hydrates_one_complete_owner_scoped_snapshot() -> None:
    source = ACCOUNTS_TAB.read_text(encoding="utf-8")

    assert "const datasetQ = useFrontendDatasetCache();" in source
    assert "const localAccountTab = datasetQ.data?.accountTabCache;" in source
    assert "remoteAccountTab?.ownerKey === ownerKey" in source
    for value in (
        "accountTab?.balances ?? []",
        "accountTab?.accounts ?? []",
        "accountTab?.cards ?? []",
        "accountTab?.manualAccounts ?? []",
    ):
        assert value in source
    for mixed_fallback in (
        "balancesQ.data ?? localAccountTab",
        "bankAccountsQ.data ?? localAccountTab",
        "cardsQ.data ?? localAccountTab",
        "manualAccountsQ.data ?? localAccountTab",
    ):
        assert mixed_fallback not in source


def test_accounts_tab_queries_and_persistence_are_owner_bound() -> None:
    source = ACCOUNTS_TAB.read_text(encoding="utf-8")

    for query_key in (
        "['portfolio', 'accounts', ownerKey, ownerEpoch]",
        "['accounts', ownerKey, ownerEpoch]",
        "['cards', ownerKey, ownerEpoch]",
        "['financial-accounts', 'manual', ownerKey, ownerEpoch]",
        "['sync', 'jobs', ownerKey, ownerEpoch]",
    ):
        assert query_key in source
    owner_hook = OWNER_HOOK.read_text(encoding="utf-8")
    dataset_hook = DATASET_HOOK.read_text(encoding="utf-8")
    assert "guardReplicaOwnerRequest" in owner_hook
    assert "skipAuthRetry: true" in owner_hook
    assert "['frontend-dataset', 'replica', ownerKey, ownerEpoch]" in dataset_hook
    assert "await waitForReplicaOwner(ownerKey, ownerEpoch)" in dataset_hook
    assert "() => replicaStore.load(ownerKey)" in dataset_hook
    assert "synchronizedOwnerRef.current = ownerSessionKey" in dataset_hook
    assert "fetchCompleteAccountTabCache" in source
    assert "persistAccountTabCache(cache, ownerEpoch)" in source
    assert "assertReplicaOwnerEpoch(ownerKey, ownerEpoch)" in source
    assert "activeOwnerRef.current !== ownerKey" in source
    assert "remoteAccountTab.epoch === ownerEpoch" in source
    assert "throwOnError: true" in source
    assert "dataUpdateCount" in source
    assert "if (!completed) return;" in source


def test_cached_account_tab_never_blocks_on_background_remote_queries() -> None:
    source = ACCOUNTS_TAB.read_text(encoding="utf-8")

    assert "deriveAccountTabLoadStatus" in source
    assert "const accountTabLoading = accountTabStatus === 'loading';" in source
    assert "const accountTabError = accountTabStatus === 'error';" in source
    assert "Boolean(accountTab) && groups.length === 0" in source
    assert "accounts={manualAccounts}" in source
    assert "isLoading={accountTabLoading}" in source
    assert "暫時無法載入帳戶資料" in source


def test_account_mutations_and_fast_sync_update_the_durable_snapshot() -> None:
    source = ACCOUNTS_TAB.read_text(encoding="utf-8")

    assert "applyAccountTabCacheUpdate" in source
    assert "updateCachedManualAccount" in source
    assert "updateCachedBankBalance" in source
    assert "updateCachedCard" in source
    assert "'optimistic',\n        ownerEpoch," in source
    assert "'rollback',\n          ownerEpoch," in source
    assert "'confirmed',\n        ownerEpoch," in source
    assert "consumeTerminalSyncJobIds" in source
    assert "triggeredJobIdsRef.current.add(result.job_id)" in source
    assert "for (const job of result.jobs)" in source
    assert "const serverPatchRevision = serverPatchRevisionRef.current;" in source
    assert "serverPatchRevisionRef.current !== serverPatchRevision" in source
    assert "serverPatchRevisionRef.current += 1" in source
    assert "persistAccountTabCacheUpdate(updater, expectedEpoch)" in source
    assert "persistAccountTabCache(next, expectedEpoch)" not in source
    assert "optimisticMutationCountRef.current > 0" in source
    assert "phase === 'confirmed' || phase === 'durable'" in source
    assert "phase === 'rollback' || phase === 'confirmed'" in source


def test_snaptrade_queries_share_the_owner_epoch_boundary() -> None:
    source = SNAPTRADE.read_text(encoding="utf-8")

    assert "useOwnerBoundApi()" in source
    assert "['snaptrade', 'status', ownerKey, ownerEpoch]" in source
    assert "['snaptrade', 'portfolio', ownerKey, ownerEpoch]" in source
    assert "ownerApi<SnapTradeStatus>('/snaptrade/status')" in source
    assert "ownerApi<SnapTradePortfolio>('/snaptrade/portfolio')" in source
    assert "api<SnapTrade" not in source


def test_auto_debit_modal_shares_the_owner_epoch_boundary() -> None:
    source = AUTO_DEBIT.read_text(encoding="utf-8")

    assert "useOwnerBoundApi()" in source
    assert "['auto-debit', 'eligible-accounts', ownerKey, ownerEpoch]" in source
    assert "['auto-debit', 'settings', ownerKey, ownerEpoch]" in source
    assert "ownerApi<EligibleAccount[]>" in source
    assert "ownerApi<AutoDebitSetting[]>" in source
    assert "return api<" not in source
