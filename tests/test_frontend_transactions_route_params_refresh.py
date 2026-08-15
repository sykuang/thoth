"""Regression: transactions tab must refresh filters when route params change.

Expo Router tabs keep tab screens mounted. If TransactionsScreen only uses route
params as useState initializers, tapping 上海銀行 first then 匯豐 later keeps the
old selectedBanks state and the filter stays on 上海銀行. Account/card drilldown
also must reset stale client-side filters (category/search/direction/view mode)
when the user comes from the account tab again.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRANSACTIONS_TSX = ROOT / "frontend/src/app/(tabs)/transactions.tsx"
DATASET_HOOK_TS = ROOT / "frontend/src/hooks/useFrontendDatasetCache.ts"
REPLICA_CORE_TS = ROOT / "frontend/src/lib/replica.ts"
OWNER_HOOK_TS = ROOT / "frontend/src/hooks/useOwnerBoundApi.ts"


def test_transactions_route_params_sync_into_mounted_filter_state():
    src = TRANSACTIONS_TSX.read_text()

    assert "import { useMemo, useState } from 'react';" not in src
    assert "import { useEffect, useMemo, useRef, useState } from 'react';" in src
    assert "const appliedRouteSignatureRef = useRef<string | null>(null);" in src
    assert "const routeSignature = [initialBank, accountNo, cardNo, params.drilldown ?? ''].join('|');" in src
    assert "if (appliedRouteSignatureRef.current === routeSignature) return;" in src
    assert "appliedRouteSignatureRef.current = routeSignature;" in src
    assert "useLocalSearchParams<{ bank?: string; kind?: string; account_no?: string; card_no?: string; drilldown?: string }>" in src
    assert "useEffect(() => {" in src
    assert "setSelectedBanks(initialBank ? [initialBank] : [])" in src
    assert "setKind(initialKind)" not in src
    assert "}, [initialBank, accountNo, cardNo, params.drilldown]);" in src


def test_account_drilldown_resets_stale_client_side_filters_and_modes():
    src = TRANSACTIONS_TSX.read_text()

    effect = src[src.index("useEffect(() => {"):src.index("// 統一 row identity")]
    assert "if (accountNo || cardNo || typeof params.drilldown === 'string')" in effect
    assert "setCategory('')" in effect
    assert "setSubcategory('')" in effect
    assert "setSearch('')" in effect
    assert "setDirection('all')" in effect
    assert "setViewMode('list')" in effect
    assert "setGranularity('month')" in effect
    assert "setSelectedPeriod(currentPeriodKey('month'))" in effect
    assert "setDetailTxn(null)" in effect
    assert "setSelectionMode(false)" in effect
    assert "setSelectedKeys(new Set())" in effect
    assert "setBulkSheetOpen(false)" in effect


def test_transactions_tab_has_pull_to_refresh_that_forces_snapshot_refetch():
    """交易明細 tab 下拉刷新必須直接 refetch /cache/snapshot，而不是只等 stale cache。"""
    src = TRANSACTIONS_TSX.read_text()

    assert "RefreshControl," in src
    assert "|| (brokerageScopeActive && brokerageQ.isRefetching)" in src
    assert "refreshControl={" in src
    assert "<RefreshControl" in src
    assert "refreshing={transactionRefreshing}" in src
    assert "onRefresh={() => {" in src
    assert "void datasetQ.refreshSnapshot();" in src
    assert "if (brokerageScopeActive) void brokerageQ.refetch();" in src
    assert "void Promise.all([datasetQ.refreshSnapshot(), brokerageQ.refetch()]);" not in src


def test_frontend_dataset_cache_uses_owner_scoped_replica_sync():
    """交易頁先讀owner-bound local replica，再以bootstrap/pull更新；畫面filter仍全在本地。"""
    hook = DATASET_HOOK_TS.read_text()
    replica_core = REPLICA_CORE_TS.read_text()
    owner_hook = OWNER_HOOK_TS.read_text()
    screen = TRANSACTIONS_TSX.read_text()

    assert "makeReplicaOwnerKey(serverUrl, email)" in owner_hook
    assert "guardReplicaOwnerRequest" in owner_hook
    assert "loadCompleteReplicaDataset(" in hook
    assert "store.load(ownerKey)" in replica_core
    assert "syncReplica(replicaStore, ownerKey" in hook
    assert "const hydratedOwners = new Set<string>();" in hook
    assert "const firstHydration = !hydratedOwners.has(ownerSessionKey);" in hook
    assert "await waitForReplicaOwner(ownerKey, ownerEpoch)" in hook
    assert "envelope.schemaVersion !== REPLICA_SCHEMA_VERSION" in replica_core
    assert "catch (syncError)" in hook
    assert "return await refreshReplica();" in hook
    assert "'/replica/bootstrap'" in hook
    assert "'/replica/pull'" in hook
    assert "'/cache/snapshot'" not in hook
    assert "setInterval(" not in hook
    assert "useMutation(" not in hook
    assert "api<TransactionsListResponse>(`/transactions" not in screen
    assert "api<TransactionsListResponse>('/transactions" not in screen


def test_route_params_are_visible_frontend_filters_only():
    """帳戶 tab 帶來的 bank/account/card 只初始化前端 filter state，不作隱藏 backend 條件。"""
    src = TRANSACTIONS_TSX.read_text()
    raw_block = src[src.index("const rawItems = useMemo(() => {"):src.index("const transactionRefreshing =")]
    toggle_block = src[src.index("function toggleBank(b: string) {"):src.index("function clearFilters() {")]

    assert "setSelectedBanks(initialBank ? [initialBank] : [])" in src
    assert "setActiveAccountNo(accountNo)" in src
    assert "setActiveCardNo(cardNo)" in src
    assert "let items = datasetQ.data?.transactions ?? [];" in raw_block
    assert "items = items.filter((t) => selectedBanks.includes(t.bank))" in raw_block
    assert "items = items.filter((t) => t.account_no === effectiveAccountNo)" in raw_block
    assert "items = items.filter((t) => matchesCardDrilldown(t, effectiveCardNo))" in raw_block
    assert "t.kind ===" not in raw_block
    assert "setActiveAccountNo('');" in toggle_block
    assert "setActiveCardNo('');" in toggle_block


def test_route_kind_is_not_a_hidden_filter_without_visible_kind_ui():
    """params.kind 曾殘留成 hidden filter；取消 bank 後仍看不到匯豐，按清除才出現。"""
    src = TRANSACTIONS_TSX.read_text()
    raw_block = src[src.index("const rawItems = useMemo(() => {"):src.index("const transactionRefreshing =")]
    clear_area = src[src.index("function clearFilters() {"):src.index("const filteredCount =")]

    assert "const initialKind" not in src
    assert "const [kind, setKind]" not in src
    assert "params.kind ===" not in src
    assert "t.kind ===" not in raw_block
    assert "kind !== 'all'" not in clear_area


def test_hidden_account_card_scope_is_effective_only_while_drilldown_bank_selected():
    """取消/改選銀行後，即使 hidden active refs 還在，也不可再套 account/card scope。"""
    src = TRANSACTIONS_TSX.read_text()
    scope_block = src[src.index("const drilldownScopeActive = Boolean("):src.index("const rawItems = useMemo")]
    badge_area = src[src.index("const activeFilterCount ="):src.index("const brokerageAccountCount =")]

    assert "initialBank && selectedBanks.length === 1 && selectedBanks[0] === initialBank" in scope_block
    assert "const effectiveAccountNo = drilldownScopeActive ? activeAccountNo : '';" in scope_block
    assert "const effectiveCardNo = drilldownScopeActive ? activeCardNo : '';" in scope_block
    assert "Boolean(effectiveAccountNo || effectiveCardNo)" in badge_area
    assert "activeAccountNo" not in badge_area
    assert "activeCardNo" not in badge_area


def test_bank_filter_options_are_union_of_credential_and_dataset_banks():
    """明細 tab 銀行 chips 要包含已提供帳密的銀行，也要包含只有信用卡/交易資料的銀行。"""
    src = TRANSACTIONS_TSX.read_text()
    block = src[src.index("const bankAccountsQ = useQuery<BankAccount[]>"):src.index("const rawItems = useMemo")]

    assert "queryKey: ['accounts']" in block
    assert "api<BankAccount[]>('/accounts')" in block
    assert "const banks = new Set<string>();" in block
    assert "if (a.has_creds) banks.add(String(a.bank));" in block
    assert "datasetQ.data?.accounts" not in block
    assert "datasetQ.data?.cards" not in block
    assert "for (const t of datasetQ.data?.transactions ?? []) banks.add(t.bank);" in block
    assert "return Array.from(banks);" in block
    assert "return credentialBanks" not in block
    assert "return Array.from(fallbackBanks)" not in block


def test_transactions_use_replica_preferences_until_server_preferences_arrive():
    src = TRANSACTIONS_TSX.read_text()
    assert "preferencesQ.hasServerData" in src
    assert "datasetQ.data?.preferences ?? preferencesQ.data" in src

    preferences_hook = (ROOT / "frontend/src/hooks/usePreferences.ts").read_text()
    assert "updateReplicaPreferences(" in preferences_hook
    assert "variables.ownerEpoch," in preferences_hook
    assert "scope: { id: 'user-preferences' }" in preferences_hook
    guard = preferences_hook.index("assertReplicaOwnerEpoch(ownerKey, ownerEpoch);")
    request = preferences_hook.index("api<UserPreferences>('/users/me/preferences'", guard)
    assert guard < request
    assert "skipAuthRetry: true" in preferences_hook[request:]
    assert "ownerEpoch: getReplicaOwnerEpoch(ownerKey)" in preferences_hook
    assert "queryKey: ['frontend-dataset', 'replica', variables.ownerKey]" in preferences_hook
    assert "refetchType: 'all'" in preferences_hook

    api_client = (ROOT / "frontend/src/lib/api.ts").read_text()
    assert "if (res.status === 401 && init.skipAuthRetry)" in api_client
    fail_closed = api_client.index("if (res.status === 401 && init.skipAuthRetry)")
    refresh_path = api_client.index("if (res.status === 401 && !init.skipAuth)")
    assert fail_closed < refresh_path


def test_transaction_filters_are_hidden_behind_one_button_and_modal():
    """銀行／分類／搜尋不可常駐佔滿交易頁；按篩選後才顯示完整 controls。"""
    src = TRANSACTIONS_TSX.read_text()

    assert "const [filterOpen, setFilterOpen] = useState(false);" in src
    header = src[src.index("{/* Section header"):src.index("{/* Phase 8.2", src.index("{/* Section header"))]
    assert 'testID="txn-filter-open"' in header
    assert "{viewMode === 'list' && (" in header
    assert "篩選{activeFilterCount > 0 ? ` ${activeFilterCount}` : ''}" in src
    assert '<Modal\n        visible={filterOpen}' in src
    assert 'testID="txn-filter-sheet"' in src
    assert 'testID="txn-filter-done"' in src
    assert 'testID="txn-filter-clear"' in src
    assert src.count("accessible={false}") >= 2
    assert src.count("focusable={false}") >= 2
    assert "銀行 (不選=全部)" in src
    assert 'placeholder="搜尋說明 / 標籤"' in src

    main = src[src.index("{/* ===== Filters ===== */}"):src.index("{/* ===== 主表 / 載入 / 錯誤 ===== */}")]
    assert "銀行 (不選=全部)" not in main
    assert 'placeholder="搜尋說明 / 標籤"' not in main
    assert "bg-white dark:bg-ink-900 rounded-2xl p-5 mb-4 shadow-card" not in main

    clear = src[src.index("function clearFilters() {"):src.index("const filteredCount =")]
    assert "setDirection('all')" not in clear
    assert "setGranularity('month')" not in clear
    assert "setSelectedPeriod(" not in clear
