/**
 * 帳戶 Tab — MoneyBook 風 (Phase 6 → 對標使用者 2026-06-14 screenshot).
 *
 * 結構: 每家銀行 group, 內列「子帳戶」+「信用卡」,
 *   - 子帳戶 (新 endpoint /portfolio/accounts): nickname + currency 餘額 + 同步時間
 *   - 信用卡 (/cards): 卡名 + 末四碼
 *
 * Phase 8.2 C (2026-06-14): 三層 nickname 都可編輯 (inline rename modal)
 *   - bank_accounts.label (跨銀行群組名): PUT /accounts/{id}
 *   - accounts.nickname_overwrite (per-bank 帳戶名): PATCH /portfolio/accounts/{bank}/{no}/nickname
 *   - cards.nickname_overwrite (per-bank 卡片名): PATCH /cards/{bank}/{no}/nickname
 *
 *   鐵則 (對齊 transactions.description_overwrite):
 *     raw label/name/nickname 不動, 只在新欄 *_overwrite 寫覆寫值;
 *     UI 顯示 fallback overwrite || raw; ↺ 重設按鈕清成 NULL.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRouter } from 'expo-router';
import {
  ChevronRight,
  Cloud,
  CreditCard,
  Eye,
  EyeOff,
  MoreHorizontal,
  Pencil,
  Settings as SettingsIcon,
  X,
} from 'lucide-react-native';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  Text,
  TextInput,
  View,
} from 'react-native';

import { BankBadge } from '@/components/BankBadge';
import { AutoDebitSettingModal } from '@/components/AutoDebitSettingModal';
import { SnapTradeAccountsSection } from '@/components/SnapTradeSections';
import { useBreakpoint } from '@/hooks/useBreakpoint';
import { useFrontendDatasetCache } from '@/hooks/useFrontendDatasetCache';
import {
  consumeTerminalSyncJobIds,
  deriveAccountTabLoadStatus,
  fetchCompleteAccountTabCache,
  hasNewerAccountTabRevision,
  updateCachedBankBalance,
  updateCachedCard,
  updateCachedManualAccount,
  type AccountTabRevisionTuple,
} from '@/lib/accountTabCache';
import { api, ApiError, formatApiError } from '@/lib/api';
import {
  formatAbsoluteDecimalCurrency,
  formatCurrency,
} from '@/lib/currency';
import { formatRelativeTime } from '@/lib/datetime';
import { maskCardNo } from '@/lib/mask';
import {
  assertReplicaOwnerEpoch,
  type ReplicaAccountTabCache,
} from '@/lib/replica';
import {
  type BankAccount,
  type BankAccountBalance,
  type Card,
  type FinancialAccount,
  type SupportedBank,
  type SyncJob,
  type TriggerSyncResponse,
  BANK_LABELS,
} from '@/types/api';

// ============================================================
// Phase 8.2 C — 共用 inline rename modal
// ============================================================
type RenameModalProps = {
  visible: boolean;
  title: string;
  rawLabel: string;        // 銀行原文 (placeholder + 顯示原本是什麼)
  currentValue: string;    // overwrite 現值 (空字串 = 沒覆寫)
  onClose: () => void;
  onSubmit: (newValue: string | null) => void;  // null = 清空
  isSubmitting: boolean;
};

function RenameModal({
  visible,
  title,
  rawLabel,
  currentValue,
  onClose,
  onSubmit,
  isSubmitting,
}: RenameModalProps) {
  const [text, setText] = useState(currentValue);

  // 每次開都 reset 到當前值 (避免上次取消殘留)
  // 用 visible toggle 觸發 reset
  const [lastVisible, setLastVisible] = useState(visible);
  if (visible !== lastVisible) {
    if (visible) setText(currentValue);
    setLastVisible(visible);
  }

  const hasOverwrite = currentValue.length > 0;
  const trimmed = text.trim();
  const canSubmit = !isSubmitting && trimmed !== currentValue;

  const body = (
    <>
      <Text className="text-ink-900 dark:text-ink-50 text-h2 mb-1">
        {title}
      </Text>
      <Text className="text-ink-500 dark:text-ink-400 text-micro mb-3">
        原文：{rawLabel || '(空)'}
      </Text>

      <TextInput
        value={text}
        onChangeText={setText}
        placeholder={rawLabel}
        placeholderTextColor="#9ca3af"
        autoFocus
        className="border border-ink-200 dark:border-ink-700 rounded-lg px-3 py-2.5 text-body text-ink-900 dark:text-ink-50 bg-white dark:bg-ink-950 mb-3"
        testID="rename-input"
      />

      <View className="flex-row gap-2 justify-end">
        {hasOverwrite && (
          <Pressable
            onPress={() => onSubmit(null)}
            disabled={isSubmitting}
            className="px-4 py-2 rounded-lg border border-amber-300 dark:border-amber-700 active:bg-amber-50 dark:active:bg-amber-950"
            testID="rename-reset-btn"
          >
            <Text className="text-amber-700 dark:text-amber-400 text-small">
              ↺ 恢復原文
            </Text>
          </Pressable>
        )}
        <Pressable
          onPress={onClose}
          disabled={isSubmitting}
          className="px-4 py-2 rounded-lg border border-ink-200 dark:border-ink-700 active:bg-ink-100 dark:active:bg-ink-800"
          testID="rename-cancel-btn"
        >
          <Text className="text-ink-700 dark:text-ink-300 text-small">取消</Text>
        </Pressable>
        <Pressable
          onPress={() => onSubmit(trimmed)}
          disabled={!canSubmit}
          className={`px-4 py-2 rounded-lg ${
            canSubmit
              ? 'bg-brand-600 active:bg-brand-700'
              : 'bg-ink-300 dark:bg-ink-700'
          }`}
          testID="rename-submit-btn"
        >
          <Text className="text-white text-small font-semibold">
            {isSubmitting ? '儲存中…' : '儲存'}
          </Text>
        </Pressable>
      </View>
    </>
  );

  // iOS native pageSheet — 系統處理鍵盤, 永遠不撞 status bar
  if (Platform.OS === 'ios') {
    return (
      <Modal
        visible={visible}
        animationType="slide"
        presentationStyle="pageSheet"
        onRequestClose={onClose}
      >
        <View className="flex-1 bg-white dark:bg-ink-900">
          <ScrollView
            className="flex-1 px-5 pt-5"
            keyboardShouldPersistTaps="handled"
            showsVerticalScrollIndicator={false}
            contentContainerStyle={{ paddingBottom: 32 }}
            automaticallyAdjustKeyboardInsets
          >
            {body}
          </ScrollView>
        </View>
      </Modal>
    );
  }

  // web/macOS — 浮動 modal
  return (
    <Modal
      visible={visible}
      transparent
      animationType="fade"
      onRequestClose={onClose}
    >
      <Pressable
        className="flex-1 bg-black/40 items-center justify-center px-4"
        onPress={onClose}
      >
        <Pressable
          className="bg-white dark:bg-ink-900 rounded-2xl p-5 w-full max-w-md shadow-card"
          onPress={(e) => e.stopPropagation()}
        >
          {body}
        </Pressable>
      </Pressable>
    </Modal>
  );
}

// ============================================================
type AccountTabCacheUpdater = (cache: ReplicaAccountTabCache) => ReplicaAccountTabCache;
type AccountTabCacheUpdatePhase = 'optimistic' | 'rollback' | 'confirmed' | 'durable';
type ApplyAccountTabCacheUpdate = (
  updater: AccountTabCacheUpdater,
  phase: AccountTabCacheUpdatePhase,
  expectedEpoch: number,
) => void;

type BankGroup = {
  bank: string;
  accounts: BankAccountBalance[];
  cards: Card[];
  /**
   * Phase 8.5 — 「已建 cred slot 但還沒 sync」的 bank_accounts row。
   * 走 /accounts endpoint (cred 槽位)，不是 /portfolio/accounts (balance row)。
   * UI: 列在已有餘額的下方、灰色標示「未同步」+ 提供「去同步」CTA 跳 dashboard。
   */
  pendingBankAccounts: BankAccount[];
};

export default function AccountsTabScreen() {
  const router = useRouter();
  const bp = useBreakpoint();
  const isDesktop = Platform.OS === 'web' && bp.isLg;
  const qc = useQueryClient();
  const datasetQ = useFrontendDatasetCache();
  const localAccountTab = datasetQ.data?.accountTabCache;
  const {
    ownerKey,
    ownerEpoch,
    ownerApi,
    persistAccountTabCache,
    persistAccountTabCacheUpdate,
  } = datasetQ;
  const [remoteAccountTab, setRemoteAccountTab] = useState<{
    ownerKey: string;
    epoch: number;
    cache: ReplicaAccountTabCache;
  }>();
  const activeOwnerRef = useRef(ownerKey);
  const refreshRef = useRef<{
    ownerKey: string;
    epoch: number;
    promise: Promise<void>;
  } | undefined>(undefined);
  const completeRevisionsRef = useRef<{
    ownerKey: string;
    epoch: number;
    revisions: AccountTabRevisionTuple;
  }>({ ownerKey: '', epoch: -1, revisions: [0, 0, 0, 0] });
  const displayedAccountTabRef = useRef<ReplicaAccountTabCache | undefined>(undefined);
  const serverPatchRevisionRef = useRef(0);
  const optimisticMutationCountRef = useRef(0);
  const triggeredJobIdsRef = useRef<Set<number>>(new Set());
  const [refreshTick, bumpRefreshTick] = useState(0);
  useEffect(() => {
    activeOwnerRef.current = ownerKey;
    serverPatchRevisionRef.current = 0;
    optimisticMutationCountRef.current = 0;
    triggeredJobIdsRef.current.clear();
  }, [ownerEpoch, ownerKey]);
  const invalidateAccountReads = useCallback(() => {
    qc.invalidateQueries({ queryKey: ['transactions'] });
    qc.invalidateQueries({ queryKey: ['frontend-dataset'] });
    qc.invalidateQueries({ queryKey: ['portfolio'] });
    qc.invalidateQueries({ queryKey: ['cards'] });
    qc.invalidateQueries({ queryKey: ['accounts'] });
    qc.invalidateQueries({ queryKey: ['auto-debit', 'reminders'] });
  }, [qc]);

  // Sync infrastructure (Phase 8.6 — 從 dashboard 搬過來 + 改 moneybook 風)
  // - jobsQ: 監聽 /sync/jobs 輪詢 (有 running 時 2s,否則停)
  // - triggerSync: 單一 account_id sync (per-bank action 觸發)
  // - triggerSyncAll: 全部同步 (header action 觸發)
  const jobsQ = useQuery<SyncJob[]>({
    queryKey: ['sync', 'jobs', ownerKey, ownerEpoch],
    queryFn: () => ownerApi<SyncJob[]>('/sync/jobs'),
    enabled: Boolean(ownerKey),
    refetchInterval: (q) => {
      const data = q.state.data;
      if (!data) return false;
      return data.some((j) => j.status === 'queued' || j.status === 'running') ? 2000 : false;
    },
  });

  // Phase C-fe Warning #3 (2026-06-17): 偵測 sync running → done transition,
  // 自動 invalidate 下游 query (transactions/portfolio/cards/accounts).
  // 否則 sync 完 user 看不到新資料, 要手動 pull-to-refresh 或切 tab.
  const prevHasRunningRef = useRef(false);
  const hasRunning = (jobsQ.data ?? []).some(
    (j) => j.status === 'queued' || j.status === 'running',
  );
  useEffect(() => {
    if (prevHasRunningRef.current && !hasRunning) {
      qc.invalidateQueries({ queryKey: ['transactions'] });
      qc.invalidateQueries({ queryKey: ['frontend-dataset'] });
      qc.invalidateQueries({ queryKey: ['portfolio'] });
      qc.invalidateQueries({ queryKey: ['cards'] });
      qc.invalidateQueries({ queryKey: ['accounts'] });
      qc.invalidateQueries({ queryKey: ['auto-debit', 'reminders'] });
    }
    prevHasRunningRef.current = hasRunning;
  }, [hasRunning, qc]);
  useEffect(() => {
    if (triggeredJobIdsRef.current.size === 0 || !jobsQ.data) return;
    const result = consumeTerminalSyncJobIds(triggeredJobIdsRef.current, jobsQ.data);
    triggeredJobIdsRef.current = result.remaining;
    if (result.reachedTerminalState) invalidateAccountReads();
  }, [invalidateAccountReads, jobsQ.data]);

  const triggerSync = useMutation<TriggerSyncResponse, ApiError, number>({
    mutationFn: (accountId) => {
      assertReplicaOwnerEpoch(ownerKey, ownerEpoch);
      return ownerApi<TriggerSyncResponse>(`/sync/account/${accountId}`, {
        method: 'POST',
        body: { headless: true },
      });
    },
    onSuccess: (result) => {
      triggeredJobIdsRef.current.add(result.job_id);
      qc.invalidateQueries({ queryKey: ['sync', 'jobs'] });
    },
  });

  const triggerSyncAll = useMutation<
    {
      queued: number;
      skipped: number;
      jobs: TriggerSyncResponse[];
    },
    ApiError,
    void
  >({
    mutationFn: () => {
      assertReplicaOwnerEpoch(ownerKey, ownerEpoch);
      return ownerApi('/sync/all', {
        method: 'POST',
        body: { headless: true },
      });
    },
    onSuccess: (result) => {
      for (const job of result.jobs) triggeredJobIdsRef.current.add(job.job_id);
      qc.invalidateQueries({ queryKey: ['sync', 'jobs'] });
    },
  });

  // /portfolio/accounts — 真實帳戶餘額 (每帳戶 1 row, sync 後才有)
  const balancesQ = useQuery<BankAccountBalance[], ApiError>({
    queryKey: ['portfolio', 'accounts', ownerKey, ownerEpoch],
    queryFn: () => ownerApi<BankAccountBalance[]>('/portfolio/accounts'),
    enabled: Boolean(ownerKey) && datasetQ.isFetched,
  });

  // /accounts — 已建立的 cred 槽位 (即使還沒 sync 也有 row)
  // 為了解決「使用者剛新增帳號後到帳戶 tab 看到空白」的反直覺，需要把
  // 「已建但未同步」的銀行也顯示出來。差集邏輯在下方 groupMap 組裝時做。
  const bankAccountsQ = useQuery<BankAccount[], ApiError>({
    queryKey: ['accounts', ownerKey, ownerEpoch],
    queryFn: () => ownerApi<BankAccount[]>('/accounts'),
    enabled: Boolean(ownerKey) && datasetQ.isFetched,
  });

  const cardsQ = useQuery<Card[], ApiError>({
    queryKey: ['cards', ownerKey, ownerEpoch],
    queryFn: () => ownerApi<Card[]>('/cards'),
    enabled: Boolean(ownerKey) && datasetQ.isFetched,
    retry: false,
  });

  const manualAccountsQ = useQuery<FinancialAccount[], ApiError>({
    queryKey: ['financial-accounts', 'manual', ownerKey, ownerEpoch],
    queryFn: () => ownerApi<FinancialAccount[]>('/financial-accounts?source=manual'),
    enabled: Boolean(ownerKey) && datasetQ.isFetched,
  });

  const refetchBalances = balancesQ.refetch;
  const refetchBankAccounts = bankAccountsQ.refetch;
  const refetchCards = cardsQ.refetch;
  const refetchManualAccounts = manualAccountsQ.refetch;

  const refreshAccountTab = useCallback((): Promise<void> => {
    if (!ownerKey || !datasetQ.isFetched || optimisticMutationCountRef.current > 0) {
      return Promise.resolve();
    }
    try {
      assertReplicaOwnerEpoch(ownerKey, ownerEpoch);
    } catch {
      return Promise.resolve();
    }
    const inFlight = refreshRef.current;
    if (inFlight?.ownerKey === ownerKey && inFlight.epoch === ownerEpoch) {
      return inFlight.promise;
    }

    const revisions: AccountTabRevisionTuple = [0, 0, 0, 0];
    const serverPatchRevision = serverPatchRevisionRef.current;
    let completed = false;
    const promise = fetchCompleteAccountTabCache({
      balances: async () => {
        const result = await refetchBalances({ cancelRefetch: false, throwOnError: true });
        if (!result.data) throw new Error('Account balances refresh returned no data');
        revisions[0] = qc.getQueryState(
          ['portfolio', 'accounts', ownerKey, ownerEpoch],
        )?.dataUpdateCount ?? 0;
        return result.data;
      },
      accounts: async () => {
        const result = await refetchBankAccounts({ cancelRefetch: false, throwOnError: true });
        if (!result.data) throw new Error('Bank accounts refresh returned no data');
        revisions[1] = qc.getQueryState(['accounts', ownerKey, ownerEpoch])?.dataUpdateCount ?? 0;
        return result.data;
      },
      cards: async () => {
        const result = await refetchCards({ cancelRefetch: false, throwOnError: true });
        if (!result.data) throw new Error('Cards refresh returned no data');
        revisions[2] = qc.getQueryState(['cards', ownerKey, ownerEpoch])?.dataUpdateCount ?? 0;
        return result.data;
      },
      manualAccounts: async () => {
        const result = await refetchManualAccounts({ cancelRefetch: false, throwOnError: true });
        if (!result.data) throw new Error('Manual accounts refresh returned no data');
        revisions[3] = qc.getQueryState(
          ['financial-accounts', 'manual', ownerKey, ownerEpoch],
        )?.dataUpdateCount ?? 0;
        return result.data;
      },
    }).then((cache) => {
      assertReplicaOwnerEpoch(ownerKey, ownerEpoch);
      if (activeOwnerRef.current !== ownerKey) return;
      if (optimisticMutationCountRef.current > 0) return;
      if (serverPatchRevisionRef.current !== serverPatchRevision) {
        bumpRefreshTick((value) => value + 1);
        return;
      }
      completed = true;
      completeRevisionsRef.current = { ownerKey, epoch: ownerEpoch, revisions };
      setRemoteAccountTab({ ownerKey, epoch: ownerEpoch, cache });
      void persistAccountTabCache(cache, ownerEpoch).catch(() => {
        // Server truth stays visible even if the local snapshot write fails.
      });
    }).catch(() => {
      // Keep the previous complete snapshot when any authoritative read fails.
    }).finally(() => {
      const activeRefresh = refreshRef.current;
      if (activeRefresh?.ownerKey !== ownerKey || activeRefresh.epoch !== ownerEpoch) return;
      refreshRef.current = undefined;
      if (!completed) return;
      try {
        assertReplicaOwnerEpoch(ownerKey, ownerEpoch);
      } catch {
        return;
      }
      const current: AccountTabRevisionTuple = [
        qc.getQueryState(['portfolio', 'accounts', ownerKey, ownerEpoch])?.dataUpdateCount ?? 0,
        qc.getQueryState(['accounts', ownerKey, ownerEpoch])?.dataUpdateCount ?? 0,
        qc.getQueryState(['cards', ownerKey, ownerEpoch])?.dataUpdateCount ?? 0,
        qc.getQueryState(
          ['financial-accounts', 'manual', ownerKey, ownerEpoch],
        )?.dataUpdateCount ?? 0,
      ];
      if (activeOwnerRef.current === ownerKey
        && hasNewerAccountTabRevision(current, revisions)) {
        bumpRefreshTick((value) => value + 1);
      }
    });
    refreshRef.current = { ownerKey, epoch: ownerEpoch, promise };
    return promise;
  }, [
    datasetQ.isFetched,
    ownerEpoch,
    ownerKey,
    persistAccountTabCache,
    qc,
    refetchBalances,
    refetchBankAccounts,
    refetchCards,
    refetchManualAccounts,
  ]);

  useEffect(() => {
    void refreshAccountTab();
  }, [refreshAccountTab, refreshTick]);

  useEffect(() => {
    const inFlight = refreshRef.current;
    if (!ownerKey || (inFlight?.ownerKey === ownerKey && inFlight.epoch === ownerEpoch)) return;
    const revisions: AccountTabRevisionTuple = [
      qc.getQueryState(['portfolio', 'accounts', ownerKey, ownerEpoch])?.dataUpdateCount ?? 0,
      qc.getQueryState(['accounts', ownerKey, ownerEpoch])?.dataUpdateCount ?? 0,
      qc.getQueryState(['cards', ownerKey, ownerEpoch])?.dataUpdateCount ?? 0,
      qc.getQueryState(['financial-accounts', 'manual', ownerKey, ownerEpoch])?.dataUpdateCount ?? 0,
    ];
    const complete = completeRevisionsRef.current;
    if (complete.ownerKey !== ownerKey
      || complete.epoch !== ownerEpoch
      || hasNewerAccountTabRevision(revisions, complete.revisions)) {
      void refreshAccountTab();
    }
  }, [
    balancesQ.data,
    bankAccountsQ.data,
    cardsQ.data,
    manualAccountsQ.data,
    ownerEpoch,
    ownerKey,
    qc,
    refreshAccountTab,
  ]);

  const accountTab = remoteAccountTab?.ownerKey === ownerKey
    && remoteAccountTab.epoch === ownerEpoch
    ? remoteAccountTab.cache
    : localAccountTab;
  useEffect(() => {
    displayedAccountTabRef.current = accountTab;
  }, [accountTab, ownerEpoch, ownerKey]);
  const applyAccountTabCacheUpdate = useCallback<ApplyAccountTabCacheUpdate>((
    updater,
    phase,
    expectedEpoch,
  ) => {
    if (!ownerKey || activeOwnerRef.current !== ownerKey) return;
    try {
      assertReplicaOwnerEpoch(ownerKey, expectedEpoch);
    } catch {
      return;
    }
    if (phase === 'optimistic') {
      optimisticMutationCountRef.current += 1;
    } else {
      if (phase === 'rollback' || phase === 'confirmed') {
        optimisticMutationCountRef.current = Math.max(0, optimisticMutationCountRef.current - 1);
      }
      bumpRefreshTick((value) => value + 1);
    }
    const current = displayedAccountTabRef.current;
    if (!current) return;
    const next = updater(current);
    const persist = phase === 'confirmed' || phase === 'durable';
    if (persist) serverPatchRevisionRef.current += 1;
    displayedAccountTabRef.current = next;
    setRemoteAccountTab({ ownerKey, epoch: expectedEpoch, cache: next });
    if (persist) {
      void persistAccountTabCacheUpdate(updater, expectedEpoch).catch(() => {
        // The server-confirmed UI patch remains visible if disk persistence fails.
      });
    }
  }, [ownerKey, persistAccountTabCacheUpdate]);
  const balances = accountTab?.balances ?? [];
  const cards = accountTab?.cards ?? [];
  const bankAccounts = accountTab?.accounts ?? [];
  const manualAccounts = accountTab?.manualAccounts ?? [];
  const accountTabStatus = deriveAccountTabLoadStatus(
    accountTab,
    { isFetched: datasetQ.isFetched, isError: datasetQ.isError },
    [balancesQ, bankAccountsQ, cardsQ, manualAccountsQ],
  );
  const accountTabLoading = accountTabStatus === 'loading';
  const accountTabError = accountTabStatus === 'error';

  // 依銀行分組 — 整本 tab 的核心
  const groupMap = new Map<string, BankGroup>();
  const ensureGroup = (bank: string): BankGroup => {
    if (!groupMap.has(bank)) {
      groupMap.set(bank, { bank, accounts: [], cards: [], pendingBankAccounts: [] });
    }
    return groupMap.get(bank)!;
  };
  for (const a of balances) {
    ensureGroup(a.bank).accounts.push(a);
  }
  for (const c of cards) {
    ensureGroup(c.bank).cards.push(c);
  }
  // 「已建但未同步」差集：bank_accounts 內，但該銀行在 balances/cards 都沒對應 row。
  // 邏輯：以「銀行」為粒度判斷（不是 per-account），因為一家銀行 sync 一次會同時
  // 抓回所有 accounts + cards。bank_accounts 該銀行有 N 筆但 balances/cards 該銀行
  // 0 筆 = 還沒 sync 過。
  const banksWithData = new Set<string>();
  for (const a of balances) banksWithData.add(a.bank);
  for (const c of cards) banksWithData.add(c.bank);
  for (const ba of bankAccounts) {
    if (!banksWithData.has(ba.bank)) {
      ensureGroup(ba.bank).pendingBankAccounts.push(ba);
    }
  }
  const groups = Array.from(groupMap.values()).sort((a, b) => {
    // 排序: 有餘額的銀行在前, 都沒餘額按字母
    const aSum = a.accounts.reduce((s, x) => s + (x.balance ?? 0), 0);
    const bSum = b.accounts.reduce((s, x) => s + (x.balance ?? 0), 0);
    if (aSum !== bSum) return bSum - aSum;
    return a.bank.localeCompare(b.bank);
  });

  // === Sync helpers (Phase 8.6) ===
  // 1. lastJobByBank: 該銀行最近一筆 sync job (任一 account)
  //    bank 粒度而非 account_id 粒度，因為 BankGroupCard sync 是 per-bank action
  //    (內部會同時 sync 該銀行所有 cred account — 在 triggerBankSync 處理)
  const lastJobByBank = useMemo(() => {
    const m: Record<string, SyncJob | undefined> = {};
    for (const j of jobsQ.data ?? []) {
      const existing = m[j.bank];
      if (!existing || (j.created_at ?? '') > (existing.created_at ?? '')) {
        m[j.bank] = j;
      }
    }
    return m;
  }, [jobsQ.data]);

  // bankToAccountIds: 同銀行的所有 account_id (per-bank sync 時要逐個 trigger)
  const bankToAccountIds: Record<string, number[]> = {};
  for (const account of bankAccounts) {
    if (!bankToAccountIds[account.bank]) bankToAccountIds[account.bank] = [];
    bankToAccountIds[account.bank].push(account.id);
  }

  // 2. hasRunningJob: 任何 job queued/running → 全部同步按鈕變 disabled + 標示
  const hasRunningJob = useMemo(() => {
    return (jobsQ.data ?? []).some((j) => j.status === 'queued' || j.status === 'running');
  }, [jobsQ.data]);

  // 3. allSyncBusy: 全部同步按鈕的 disabled 條件 (mutation pending 或已有 job 在跑)
  const allSyncBusy = triggerSyncAll.isPending || hasRunningJob;

  // 4. triggerBankSync: per-bank 處理，對該銀行所有 account_id 平行觸發 sync。
  //    backend /sync/account/{id} 是 per-account,所以一家銀行 N 個 account 要 trigger N 次.
  //    triggerSync.mutate 內部已 invalidate jobs query,輪詢會自動接手.
  //
  // Phase C-fe Warning #4 (2026-06-17): 之前 for-loop 平行 mutate 多 account 時,
  // useMutation.variables 只記最後一次, syncBusy 判斷不準 (4 並發只能追到 1),
  // 連按 2 次同一銀行 = spawn 8 個 job request. 改用 syncingBanks Set 顯式追蹤,
  // trigger 期間禁按.
  const [syncingBanks, setSyncingBanks] = useState<Set<string>>(new Set());
  const triggerBankSync = async (bank: string) => {
    if (syncingBanks.has(bank)) return;  // 已在 trigger 期間, 禁重複
    const ids = bankToAccountIds[bank] ?? [];
    if (ids.length === 0) return;
    setSyncingBanks((prev) => new Set(prev).add(bank));
    try {
      await Promise.allSettled(
        ids.map((id) => triggerSync.mutateAsync(id)),
      );
    } finally {
      setSyncingBanks((prev) => {
        const next = new Set(prev);
        next.delete(bank);
        return next;
      });
    }
  };

  return (
    <ScrollView
      className="flex-1 bg-ink-50 dark:bg-ink-950"
      contentInsetAdjustmentBehavior="automatic"
      contentContainerStyle={{ paddingBottom: 32 }}
      bounces={false}
    >
      <View className="px-4 py-6 max-w-[1180px] w-full mx-auto">
        {/* Header — 帳戶新增與同步的單一全域入口 */}
        <View className="flex-row items-start mb-1">
          <View className="flex-1">
            <Text className="text-ink-900 dark:text-ink-50 text-h1 mb-1">帳戶</Text>
          </View>
          <View className="flex-row gap-2">
            <Pressable
              onPress={() => router.push('/(tabs)/cards/add')}
              accessibilityRole="button"
              accessibilityLabel="新增帳戶"
              className="bg-brand-600 active:bg-brand-700 rounded-xl px-4 py-2 shadow-brand"
              testID="add-account-btn"
            >
              <Text className="text-white text-small font-semibold">＋ 新增</Text>
            </Pressable>
            {groups.length > 0 && (
              <Pressable
                onPress={() => triggerSyncAll.mutate()}
                disabled={allSyncBusy}
                className={`bg-accent-600 dark:bg-accent-500 active:bg-accent-700 dark:active:bg-accent-600 rounded-xl px-4 py-2 shadow-brand ${
                  allSyncBusy ? 'opacity-40' : ''
                }`}
                testID="sync-all-btn"
              >
                {triggerSyncAll.isPending ? (
                  <View className="flex-row items-center gap-2">
                    <ActivityIndicator color="#fff" size="small" />
                    <Text className="text-white text-small font-semibold">啟動中…</Text>
                  </View>
                ) : hasRunningJob ? (
                  <View className="flex-row items-center gap-2">
                    <ActivityIndicator color="#fff" size="small" />
                    <Text className="text-white text-small font-semibold">同步中…</Text>
                  </View>
                ) : (
                  <View className="flex-row items-center gap-2">
                    <Cloud size={16} color="#fff" strokeWidth={2.4} />
                    <Text className="text-white text-small font-semibold">全部同步</Text>
                  </View>
                )}
              </Pressable>
            )}
          </View>
        </View>
        <Text className="text-ink-500 dark:text-ink-400 text-small mb-6">
          管理銀行帳戶、信用卡與已連結券商。銀行資料可從各銀行卡片同步，券商資料列於下方。
        </Text>

        {triggerSync.isError && (
          <View className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-xl px-4 py-3 mb-3">
            <Text className="text-red-700 dark:text-red-300 text-small">
              同步失敗: {formatApiError(triggerSync.error)}
            </Text>
          </View>
        )}
        {triggerSyncAll.isError && (
          <View className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-xl px-4 py-3 mb-3">
            <Text className="text-red-700 dark:text-red-300 text-small">
              全部同步失敗: {formatApiError(triggerSyncAll.error)}
            </Text>
          </View>
        )}

        {accountTabError && (
          <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-4">
            <Text className="text-ink-700 dark:text-ink-200 text-small mb-3">
              暫時無法載入帳戶資料，請稍後再試。
            </Text>
            <Pressable
              className="self-start bg-brand-600 active:bg-brand-700 rounded-xl px-4 py-2"
              onPress={() => { void refreshAccountTab(); }}
            >
              <Text className="text-white text-small font-semibold">重新載入</Text>
            </Pressable>
          </View>
        )}

        {accountTabLoading ? (
          <View className="bg-white dark:bg-ink-900 rounded-2xl p-6 shadow-card">
            <ActivityIndicator />
          </View>
        ) : Boolean(accountTab) && groups.length === 0 ? (
          <View className="bg-white dark:bg-ink-900 rounded-2xl p-8 items-center shadow-card">
            <Text className="text-ink-400 dark:text-ink-500 text-h3 mb-1">還沒任何銀行帳戶資料</Text>
            <Text className="text-ink-500 dark:text-ink-400 text-small text-center">
              點上方「新增」連結銀行或建立手動帳戶
            </Text>
          </View>
        ) : (
          <View className={isDesktop ? 'flex-row flex-wrap gap-4' : ''}>
            {groups.map((g) => (
              <View
                key={g.bank}
                style={isDesktop ? { flexBasis: '48%', flexGrow: 0, flexShrink: 1 } : undefined}
              >
                <BankGroupCard
                  group={g}
                  lastJob={lastJobByBank[g.bank]}
                  onSync={() => triggerBankSync(g.bank)}
                  syncBusy={syncingBanks.has(g.bank)}
                  ownerKey={ownerKey}
                  ownerEpoch={ownerEpoch}
                  applyAccountTabCacheUpdate={applyAccountTabCacheUpdate}
                />
              </View>
            ))}
          </View>
        )}
        {!accountTabError && (
          <ManualAccountsSection
            accounts={manualAccounts}
            isLoading={accountTabLoading}
            ownerKey={ownerKey}
            ownerEpoch={ownerEpoch}
            applyAccountTabCacheUpdate={applyAccountTabCacheUpdate}
          />
        )}
        <SnapTradeAccountsSection />
      </View>
    </ScrollView>
  );
}

function ManualAccountsSection({
  accounts,
  isLoading,
  ownerKey,
  ownerEpoch,
  applyAccountTabCacheUpdate,
}: {
  accounts: FinancialAccount[];
  isLoading: boolean;
  ownerKey: string;
  ownerEpoch: number;
  applyAccountTabCacheUpdate: ApplyAccountTabCacheUpdate;
}) {
  const typeLabel: Record<string, string> = {
    deposit: '存款',
    time_deposit: '定存',
    fx_deposit: '外幣存款',
    checking: '支票存款',
    loan: '貸款',
    mortgage: '房貸',
    credit_line: '信用額度',
    investment: '投資',
  };
  return (
    <View className="mt-5">
      <View className="mb-3">
        <Text className="text-ink-900 dark:text-ink-50 text-h2">手動帳戶</Text>
        <Text className="text-ink-500 dark:text-ink-400 text-micro mt-0.5">
          存款、貸款與投資持股／交易
        </Text>
      </View>
      {isLoading ? (
        <View className="bg-white dark:bg-ink-900 rounded-2xl p-5">
          <ActivityIndicator />
        </View>
      ) : accounts.length === 0 ? (
        <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 border border-dashed border-ink-300 dark:border-ink-700">
          <Text className="text-ink-500 dark:text-ink-400 text-small text-center">
            尚未建立手動帳戶
          </Text>
        </View>
      ) : accounts.map((account) => (
        <ManualAccountRow
          key={account.id}
          account={account}
          typeLabel={typeLabel[account.product_type] ?? account.product_type}
          ownerKey={ownerKey}
          ownerEpoch={ownerEpoch}
          applyAccountTabCacheUpdate={applyAccountTabCacheUpdate}
        />
      ))}
    </View>
  );
}

function ManualAccountRow({
  account,
  typeLabel,
  ownerKey,
  ownerEpoch,
  applyAccountTabCacheUpdate,
}: {
  account: FinancialAccount;
  typeLabel: string;
  ownerKey: string;
  ownerEpoch: number;
  applyAccountTabCacheUpdate: ApplyAccountTabCacheUpdate;
}) {
  const router = useRouter();
  const qc = useQueryClient();
  const excluded = !account.included_in_net_worth;
  const isLiability =
    account.product_type === 'loan'
    || account.product_type === 'mortgage'
    || account.product_type === 'credit_line'
    || account.balance?.trim().startsWith('-') === true;
  const toggleMut = useMutation({
    mutationFn: (next: boolean) => {
      assertReplicaOwnerEpoch(ownerKey, ownerEpoch);
      return api(
        `/financial-accounts/${account.id}/included`,
        {
          method: 'PATCH',
          body: { included_in_net_worth: next },
          skipAuthRetry: true,
        },
      );
    },
    onMutate: (next: boolean) => {
      applyAccountTabCacheUpdate(
        (cache) => updateCachedManualAccount(cache, account.id, {
          included_in_net_worth: next,
        }),
        'optimistic',
        ownerEpoch,
      );
      return { previous: account.included_in_net_worth };
    },
    onError: (_error, _next, context) => {
      if (context) {
        applyAccountTabCacheUpdate(
          (cache) => updateCachedManualAccount(cache, account.id, {
            included_in_net_worth: context.previous,
          }),
          'rollback',
          ownerEpoch,
        );
      }
    },
    onSuccess: (_result, next) => {
      applyAccountTabCacheUpdate(
        (cache) => updateCachedManualAccount(cache, account.id, {
          included_in_net_worth: next,
        }),
        'confirmed',
        ownerEpoch,
      );
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['financial-accounts'] });
      qc.invalidateQueries({ queryKey: ['portfolio', 'summary'] });
    },
  });

  return (
    <View
      className={`bg-white dark:bg-ink-900 rounded-2xl shadow-card mb-3 flex-row items-stretch ${
        excluded ? 'opacity-50' : ''
      }`}
      testID={`manual-account-${account.id}`}
    >
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`開啟手動帳戶 ${account.name}`}
        onPress={() => router.push({
          pathname: '/(tabs)/cards/manual/[account_id]',
          params: { account_id: account.id },
        })}
        className="flex-1 min-w-0 px-4 py-3 active:opacity-80"
      >
        <View className="flex-row items-baseline justify-between gap-3">
          <View className="flex-1 min-w-0">
            <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold" numberOfLines={1}>
              {account.name}
            </Text>
            <Text className="text-ink-500 dark:text-ink-400 text-micro mt-0.5" numberOfLines={1}>
              {typeLabel}
              {account.product_type === 'investment' && account.valuation_source === 'yahoo_finance'
                ? ' · Yahoo 市值'
                : account.product_type === 'investment' && account.valuation_source === 'manual_fallback'
                  ? ' · Yahoo 查價失敗，顯示手動估值'
                  : ''}
              {excluded ? ' · 未列入' : ''}
            </Text>
          </View>
          <Text className={`text-small font-semibold ${excluded
            ? 'text-ink-400 dark:text-ink-500 line-through'
            : isLiability
              ? 'text-red-600 dark:text-red-400'
              : 'text-emerald-600 dark:text-emerald-400'}`}>
            {account.balance == null
              ? '—'
              : (formatAbsoluteDecimalCurrency(account.balance, account.currency) ?? '—')}
          </Text>
        </View>
      </Pressable>
      <Pressable
        onPress={() => toggleMut.mutate(excluded)}
        disabled={toggleMut.isPending}
        accessibilityRole="button"
        accessibilityState={{ disabled: toggleMut.isPending }}
        className="w-12 items-center justify-center active:bg-ink-100 dark:active:bg-ink-800"
        testID={`manual-account-toggle-${account.id}`}
        accessibilityLabel={excluded ? '納入淨資產統計' : '不納入淨資產統計'}
      >
        {excluded
          ? <EyeOff size={20} color="#94a3b8" strokeWidth={2.2} />
          : <Eye size={20} color="#64748b" strokeWidth={2.2} />}
      </Pressable>
    </View>
  );
}

// ============================================================
// 單一銀行 card (MoneyBook 風 — 整張白底卡, 內含 header + 子帳戶 list)
// ============================================================
function BankGroupCard({
  group,
  lastJob,
  onSync,
  syncBusy,
  ownerKey,
  ownerEpoch,
  applyAccountTabCacheUpdate,
}: {
  group: BankGroup;
  lastJob: SyncJob | undefined;
  onSync: () => void;
  syncBusy: boolean;
  ownerKey: string;
  ownerEpoch: number;
  applyAccountTabCacheUpdate: ApplyAccountTabCacheUpdate;
}) {
  const router = useRouter();
  const bankLabel = BANK_LABELS[group.bank as SupportedBank] ?? group.bank;
  const [autoDebitOpen, setAutoDebitOpen] = useState(false);
  const [actionsOpen, setActionsOpen] = useState(false);

  // 同步狀態 (Phase 8.6)
  const isSyncing = syncBusy || lastJob?.status === 'queued' || lastJob?.status === 'running';
  const isFailed = lastJob?.status === 'failed';
  const lastDoneAt =
    lastJob?.status === 'done' && lastJob?.finished_at ? lastJob.finished_at : undefined;

  return (
    <View
      className="bg-white dark:bg-ink-900 rounded-2xl shadow-card mb-4 overflow-hidden"
      testID={`bank-group-${group.bank}`}
    >
      {/* Bank header — primary sync plus one low-frequency actions menu. */}
      <View
        className="flex-row items-center gap-2 px-4 py-3 border-b border-ink-100 dark:border-ink-800"
      >
        <BankBadge bank={group.bank} size="sm" />
        <View className="flex-1">
          <Text className="text-ink-900 dark:text-ink-50 text-h3" numberOfLines={1}>
            {bankLabel}
          </Text>
          {/* 同步狀態 — 失敗紅色,完成 X 分鐘前,進行中 spinner */}
          {isFailed ? (
            <Text className="text-red-600 dark:text-red-400 text-micro mt-0.5" numberOfLines={1}>
              ⚠️ 上次同步失敗{lastJob?.error_msg ? ` — ${lastJob.error_msg}` : ''}
            </Text>
          ) : isSyncing ? (
            <Text className="text-accent-600 dark:text-accent-500 text-micro mt-0.5">
              同步進行中…
            </Text>
          ) : lastDoneAt ? (
            <Text className="text-ink-500 dark:text-ink-400 text-micro mt-0.5">
              上次同步 {formatRelativeTime(lastDoneAt)}
            </Text>
          ) : (
            <Text className="text-ink-400 dark:text-ink-500 text-micro mt-0.5">尚未同步</Text>
          )}
        </View>
        <Pressable
          onPress={onSync}
          disabled={isSyncing}
          className={`w-9 h-9 items-center justify-center rounded-full active:bg-ink-100 dark:active:bg-ink-800 ${
            isSyncing ? 'opacity-40' : ''
          }`}
          testID={`bank-sync-${group.bank}`}
          accessibilityLabel={`同步 ${bankLabel}`}
        >
          {isSyncing ? (
            <ActivityIndicator size="small" />
          ) : (
            <Cloud size={20} color={isFailed ? '#dc2626' : '#64748b'} strokeWidth={2.2} />
          )}
        </Pressable>
        <Pressable
          onPress={() => setActionsOpen(true)}
          className="w-9 h-9 items-center justify-center rounded-full active:bg-ink-100 dark:active:bg-ink-800"
          testID={`bank-actions-${group.bank}`}
          accessibilityLabel={`${bankLabel} 更多操作`}
        >
          <MoreHorizontal size={21} color="#64748b" strokeWidth={2.2} />
        </Pressable>
      </View>

      {/* 子帳戶 list (存款帳戶) */}
      {group.accounts.map((a, idx) => (
        <AccountRow
          key={`a-${a.account_no}-${a.currency}`}
          account={a}
          ownerKey={ownerKey}
          ownerEpoch={ownerEpoch}
          applyAccountTabCacheUpdate={applyAccountTabCacheUpdate}
          isLast={
            idx === group.accounts.length - 1 &&
            group.cards.length === 0 &&
            group.pendingBankAccounts.length === 0
          }
        />
      ))}

      {/* 信用卡 list */}
      {group.cards.map((c, idx) => (
        <CardRow
          key={`c-${c.card_no}`}
          card={c}
          ownerKey={ownerKey}
          ownerEpoch={ownerEpoch}
          applyAccountTabCacheUpdate={applyAccountTabCacheUpdate}
          isLast={idx === group.cards.length - 1 && group.pendingBankAccounts.length === 0}
        />
      ))}

      {/* Phase 8.5 — 已建但還沒同步的 cred slot (走 /accounts 不是 /portfolio/accounts) */}
      {group.pendingBankAccounts.map((ba, idx) => (
        <PendingBankAccountRow
          key={`p-${ba.id}`}
          account={ba}
          isLast={idx === group.pendingBankAccounts.length - 1}
          lastJob={lastJob}
          onSync={onSync}
          syncBusy={isSyncing}
        />
      ))}

      {/* 三種 list (accounts/cards/pending) 全空才顯示「空」hint
          (理論上很少發生 — group 能進來就代表 cards 有資料；
           保留只是 defensive UX) */}
      {group.accounts.length === 0 &&
        group.cards.length === 0 &&
        group.pendingBankAccounts.length === 0 && (
          <View className="px-4 py-5">
            <Text className="text-ink-400 dark:text-ink-500 text-small text-center">
              (此銀行還沒抓到帳戶或卡片)
            </Text>
          </View>
        )}

      <Modal
        visible={actionsOpen}
        transparent
        animationType={Platform.OS === 'ios' ? 'slide' : 'fade'}
        onRequestClose={() => setActionsOpen(false)}
      >
        <Pressable
          className="flex-1 bg-black/40 justify-end web:items-center web:justify-center web:p-4"
          onPress={() => setActionsOpen(false)}
        >
          <Pressable
            className="bg-white dark:bg-ink-900 rounded-t-3xl web:rounded-2xl w-full max-w-md p-5 shadow-pop"
            onPress={(event) => event.stopPropagation()}
            testID={`bank-actions-menu-${group.bank}`}
            accessibilityViewIsModal
            accessibilityLabel={`${bankLabel} 操作選單`}
          >
            <View className="flex-row items-center mb-3">
              <Text className="text-ink-900 dark:text-ink-50 text-h3 flex-1">{bankLabel} 操作</Text>
              <Pressable
                onPress={() => setActionsOpen(false)}
                className="w-9 h-9 items-center justify-center rounded-full active:bg-ink-100 dark:active:bg-ink-800"
                accessibilityRole="button"
                accessibilityLabel="關閉操作選單"
              >
                <X size={19} color="#64748b" strokeWidth={2.2} />
              </Pressable>
            </View>
            {group.cards.length > 0 && (
              <Pressable
                onPress={() => {
                  setActionsOpen(false);
                  setAutoDebitOpen(true);
                }}
                className="flex-row items-center gap-3 py-3 border-b border-ink-100 dark:border-ink-800 active:opacity-60"
                testID={`bank-auto-debit-${group.bank}`}
                accessibilityRole="button"
              >
                <CreditCard size={20} color="#64748b" strokeWidth={2.2} />
                <Text className="text-ink-900 dark:text-ink-50 text-body">自動扣繳設定</Text>
              </Pressable>
            )}
            <Pressable
              onPress={() => {
                setActionsOpen(false);
                router.push(`/(tabs)/cards/credentials/${group.bank}`);
              }}
              className="flex-row items-center gap-3 py-3 active:opacity-60"
              testID={`bank-creds-${group.bank}`}
              accessibilityRole="button"
            >
              <SettingsIcon size={20} color="#64748b" strokeWidth={2.2} />
              <Text className="text-ink-900 dark:text-ink-50 text-body">管理銀行登入</Text>
            </Pressable>
          </Pressable>
        </Pressable>
      </Modal>

      <AutoDebitSettingModal
        visible={autoDebitOpen}
        onClose={() => setAutoDebitOpen(false)}
        cardBank={group.bank}
        bankLabel={bankLabel}
      />
    </View>
  );
}

// ============================================================
// Phase 8.5 — 「已建 cred slot 但還沒 sync」的 row
// ============================================================
// Phase 8.5 — 「已建 cred slot 但還沒 sync」的 row
// 灰底 + 「未同步」徽章 + 初次同步 CTA (Phase 8.6: 不再跳 dashboard,就地觸發)
// 跟一般 AccountRow 對比要明顯區分（沒餘額、沒幣別、沒最後同步時間）
// ============================================================
function PendingBankAccountRow({
  account,
  isLast,
  lastJob,
  onSync,
  syncBusy,
}: {
  account: BankAccount;
  isLast: boolean;
  lastJob: SyncJob | undefined;
  onSync: () => void;
  syncBusy: boolean;
}) {
  const completedAt = lastJob?.status === 'done' && lastJob.finished_at ? lastJob.finished_at : null;
  const failedAt = lastJob?.status === 'failed' && lastJob.finished_at ? lastJob.finished_at : null;
  const statusTone = completedAt
    ? 'bg-brand-50 dark:bg-brand-950'
    : failedAt
      ? 'bg-red-50 dark:bg-red-950'
      : 'bg-amber-50 dark:bg-amber-950';
  const badgeTone = completedAt
    ? 'bg-brand-100 dark:bg-brand-900'
    : failedAt
      ? 'bg-red-100 dark:bg-red-900'
      : 'bg-amber-100 dark:bg-amber-950';
  const badgeText = completedAt ? '已同步' : failedAt ? '同步失敗' : '未同步';
  const badgeTextTone = completedAt
    ? 'text-brand-700 dark:text-brand-300'
    : failedAt
      ? 'text-red-700 dark:text-red-300'
      : 'text-amber-700 dark:text-amber-400';
  const hint = completedAt
    ? `已於 ${formatRelativeTime(completedAt)} 同步；等待銀行資料寫入後會自動變成帳戶列`
    : failedAt
      ? `上次同步失敗${lastJob?.error_msg ? ` — ${lastJob.error_msg}` : ''}`
      : account.has_creds
        ? '已設定登入 — 按右側按鈕初次同步抓帳'
        : '尚未設定登入 — 從更多操作管理銀行登入';

  return (
    <View
      className={`flex-row items-center gap-3 px-4 py-3 ${statusTone} ${
        isLast ? '' : 'border-b border-ink-100 dark:border-ink-800'
      }`}
      testID={`pending-bank-account-${account.id}`}
    >
      <View className="flex-1">
        <View className="flex-row items-center gap-2 mb-0.5">
          <Text className="text-ink-700 dark:text-ink-300 text-body" numberOfLines={1}>
            {account.label || '預設'}
          </Text>
          <View className={`${badgeTone} px-2 py-0.5 rounded`}>
            <Text className={`${badgeTextTone} text-micro`}>{badgeText}</Text>
          </View>
        </View>
        <Text className="text-ink-500 dark:text-ink-400 text-micro">
          {hint}
        </Text>
      </View>
      {account.has_creds && (
        <Pressable
          onPress={onSync}
          disabled={syncBusy}
          className={`bg-accent-600 dark:bg-accent-500 active:bg-accent-700 rounded-lg px-3 py-1.5 ${
            syncBusy ? 'opacity-40' : ''
          }`}
          testID={`pending-bank-sync-${account.id}`}
          accessibilityLabel={`初次同步 ${account.label}`}
        >
          {syncBusy ? (
            <ActivityIndicator color="#fff" size="small" />
          ) : (
            <View className="flex-row items-center gap-1.5">
              <Cloud size={14} color="#fff" strokeWidth={2.4} />
              <Text className="text-white text-micro font-semibold">初次同步</Text>
            </View>
          )}
        </Pressable>
      )}
    </View>
  );
}

// ============================================================
// 存款帳戶 row (MoneyBook 風 — icon 圖示 + 名稱 + 餘額 + 同步時間)
// Phase 6 (2026-06-14): excluded → 整列 opacity-40 反灰 + 右側切換按鈕
// Phase 8.2 C (2026-06-14): 加暱稱編輯 — PATCH /portfolio/accounts/.../nickname
// ============================================================
function AccountRow({
  account,
  isLast,
  ownerKey,
  ownerEpoch,
  applyAccountTabCacheUpdate,
}: {
  account: BankAccountBalance;
  isLast: boolean;
  ownerKey: string;
  ownerEpoch: number;
  applyAccountTabCacheUpdate: ApplyAccountTabCacheUpdate;
}) {
  const qc = useQueryClient();
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const borderClass = isLast ? '' : 'border-b border-ink-100 dark:border-ink-800';
  const isTwd = (account.currency || 'TWD').toUpperCase() === 'TWD';
  const balanceText = account.balance == null
    ? '—'
    : formatCurrency(account.balance, account.currency);
  const hasBalance = account.balance !== null && account.balance !== undefined;
  const isLiability = account.balance != null && account.balance < 0;
  const excluded = account.excluded === true;

  // 帳戶名稱 fallback: overwrite > nickname > type > account_no
  // nickname_overwrite 只影響名稱內容；row 右側保留獨立編輯 affordance。
  const rawName = account.nickname || account.type || account.account_no;
  const overwriteName = (account.nickname_overwrite ?? '').trim();
  const displayName = overwriteName.length > 0 ? overwriteName : rawName;
  const subtitle = account.account_no;

  // 同步時間 → 相對時間 (e.g. "19 小時前")
  const syncedLabel = account.snapshot_date
    ? formatRelativeTime(account.snapshot_date)
    : null;


  // PATCH /portfolio/accounts/{bank}/{account_no}/excluded
  // 帳戶 snapshot 先樂觀反灰；server 成功後同一 delta 寫回 owner-scoped replica。
  // Aggregate 與交易資料在 settle 後走 authoritative invalidation。
  const toggleMut = useMutation({
    mutationFn: (next: boolean) => {
      assertReplicaOwnerEpoch(ownerKey, ownerEpoch);
      return api(`/portfolio/accounts/${account.bank}/${account.account_no}/excluded`, {
        method: 'PATCH',
        body: { excluded: next },
        skipAuthRetry: true,
      });
    },
    onMutate: (next: boolean) => {
      applyAccountTabCacheUpdate(
        (cache) => updateCachedBankBalance(
          cache,
          account.bank,
          account.account_no,
          { excluded: next },
        ),
        'optimistic',
        ownerEpoch,
      );
      return { previous: account.excluded };
    },
    onError: (_error, _next, context) => {
      if (context) {
        applyAccountTabCacheUpdate(
          (cache) => updateCachedBankBalance(
            cache,
            account.bank,
            account.account_no,
            { excluded: context.previous },
          ),
          'rollback',
          ownerEpoch,
        );
      }
    },
    onSuccess: (_result, next) => {
      applyAccountTabCacheUpdate(
        (cache) => updateCachedBankBalance(
          cache,
          account.bank,
          account.account_no,
          { excluded: next },
        ),
        'confirmed',
        ownerEpoch,
      );
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['portfolio', 'accounts'] });
      qc.invalidateQueries({ queryKey: ['portfolio', 'summary'] });
      qc.invalidateQueries({ queryKey: ['transactions'] });
      qc.invalidateQueries({ queryKey: ['frontend-dataset'] });
    },
  });

  // Phase 8.2 C: PATCH /portfolio/accounts/{bank}/{account_no}/nickname
  const renameMut = useMutation({
    mutationFn: (newName: string | null) => {
      assertReplicaOwnerEpoch(ownerKey, ownerEpoch);
      return api(`/portfolio/accounts/${account.bank}/${account.account_no}/nickname`, {
        method: 'PATCH',
        body: { nickname_overwrite: newName },
        skipAuthRetry: true,
      });
    },
    onSuccess: (_result, newName) => {
      applyAccountTabCacheUpdate(
        (cache) => updateCachedBankBalance(
          cache,
          account.bank,
          account.account_no,
          { nickname_overwrite: newName },
        ),
        'durable',
        ownerEpoch,
      );
      qc.invalidateQueries({ queryKey: ['portfolio', 'accounts'] });
      setEditing(false);
    },
  });

  return (
    <View
      className={`flex-row items-stretch ${borderClass} ${excluded ? 'opacity-50' : ''}`}
      testID={`account-row-${account.account_no}`}
    >
      <Pressable
        className="flex-1 min-w-0 py-3 pl-4 pr-2"
        onPress={() => router.push({
          pathname: '/(tabs)/transactions',
          params: {
            bank: account.bank,
            kind: 'twd',
            account_no: account.account_no,
            drilldown: String(Date.now()),
          },
        })}
        testID={`account-detail-${account.account_no}`}
      >
        {/* 行 1: 名稱 (左) + 餘額 (右) */}
        <View className="flex-row items-baseline justify-between">
          <View className="flex-row items-center gap-1 flex-1 min-w-0">
            <Text className="text-ink-900 dark:text-ink-50 text-body" numberOfLines={1}>
              {displayName}
            </Text>
          </View>
          {hasBalance ? (
            <Text
              className={`text-h3 font-semibold font-mono ml-2 ${excluded
                ? 'text-ink-400 dark:text-ink-500 line-through'
                : isLiability
                  ? 'text-red-600 dark:text-red-400'
                  : 'text-emerald-600 dark:text-emerald-400'}`}
              numberOfLines={1}
            >
              {balanceText}
            </Text>
          ) : (
            <Text className="text-ink-300 dark:text-ink-600 text-h3 ml-2">—</Text>
          )}
        </View>
        {/* 行 2: 帳號 (左) + 外幣估值 or 同步時間 (右) */}
        <View className="flex-row items-baseline justify-between mt-1">
          <Text className="text-ink-400 dark:text-ink-500 text-micro" numberOfLines={1}>
            {subtitle}
            {excluded && <Text className="text-ink-400 dark:text-ink-500"> · 未列入</Text>}
          </Text>
          {!isTwd && account.twd_estimate !== null && account.twd_estimate !== undefined ? (
            <Text
              className="text-ink-400 dark:text-ink-500 text-micro font-mono"
              numberOfLines={1}
            >
              ≈ {formatCurrency(account.twd_estimate, 'TWD')}
            </Text>
          ) : syncedLabel ? (
            <View className="flex-row items-center gap-1">
              {account.is_stale && (
                <Text className="text-amber-500 text-micro">⚠️</Text>
              )}
              <Text
                className={`text-micro ${account.is_stale ? 'text-amber-600 dark:text-amber-400' : 'text-ink-400 dark:text-ink-500'}`}
                numberOfLines={1}
              >
                {syncedLabel}
              </Text>
            </View>
          ) : (
            <Text className="text-ink-400 dark:text-ink-500 text-micro">無交易紀錄</Text>
          )}
        </View>
      </Pressable>

      <Pressable
        onPress={() => setEditing(true)}
        disabled={renameMut.isPending}
        className="w-8 items-center justify-center active:bg-ink-100 dark:active:bg-ink-800"
        testID={`account-rename-${account.account_no}`}
        accessibilityLabel="編輯帳戶暱稱"
      >
        <Pencil size={16} color="#94a3b8" strokeWidth={2.2} />
      </Pressable>

      {/* 排除統計 toggle */}
      <Pressable
        onPress={() => toggleMut.mutate(!excluded)}
        disabled={toggleMut.isPending}
        className="w-10 items-center justify-center active:bg-ink-100 dark:active:bg-ink-800"
        testID={`account-toggle-${account.account_no}`}
        accessibilityLabel={excluded ? '納入淨資產統計' : '不納入淨資產統計'}
      >
        {excluded
          ? <EyeOff size={20} color="#94a3b8" strokeWidth={2.2} />
          : <Eye size={20} color="#64748b" strokeWidth={2.2} />}
      </Pressable>

      <RenameModal
        visible={editing}
        title="編輯帳戶暱稱"
        rawLabel={rawName}
        currentValue={overwriteName}
        onClose={() => setEditing(false)}
        onSubmit={(v) => renameMut.mutate(v)}
        isSubmitting={renameMut.isPending}
      />
    </View>
  );
}

// ============================================================
// 信用卡 row (跟 AccountRow 同 layout, 只是 icon + 內容換成卡相關)
// === UI 鐵令 (使用者 2026-06-17) ===
//   - 兩行: (左)卡名+末四碼 / (右)額度+到期日
//   - 廢除 emoji 卡 icon、未出帳；帳單應繳移到卡片詳情頁
//   - 狀態以到期文字語意色呈現，不再讓每列共用模糊的左色條。
// Phase 6 (2026-06-14 PM): excluded → 整列反灰 + 卡名劃線 + 右側 toggle
// Phase 8.2 C (2026-06-14): 加暱稱編輯 — PATCH /cards/{bank}/{card_no}/nickname
// ============================================================
function CardRow({
  card,
  isLast,
  ownerKey,
  ownerEpoch,
  applyAccountTabCacheUpdate,
}: {
  card: Card;
  isLast: boolean;
  ownerKey: string;
  ownerEpoch: number;
  applyAccountTabCacheUpdate: ApplyAccountTabCacheUpdate;
}) {
  const qc = useQueryClient();
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const borderClass = isLast ? '' : 'border-b border-ink-100 dark:border-ink-800';
  const excluded = card.excluded === true;
  const subtitle = maskCardNo(card.card_no);
  const billDue = card.bill_due_amount ?? 0;
  const dueLabel = formatDueDateLabel(
    card.payment_due_date,
    billDue,
    card.bill_status,
  );

  // Phase 8.2 C: 顯示 fallback overwrite || raw；row 右側保留獨立編輯 affordance。
  const rawName = card.name ?? '(未命名)';
  const overwriteName = (card.nickname_overwrite ?? '').trim();
  const displayName = overwriteName.length > 0 ? overwriteName : rawName;

  // PATCH /cards/{bank}/{card_no}/excluded
  // 卡片 snapshot 先樂觀反灰；server 成功後寫回 owner-scoped replica，
  // aggregate 與交易資料在 settle 後 authoritative refresh。
  const toggleMut = useMutation({
    mutationFn: (next: boolean) => {
      assertReplicaOwnerEpoch(ownerKey, ownerEpoch);
      return api(`/cards/${card.bank}/${card.card_no}/excluded`, {
        method: 'PATCH',
        body: { excluded: next },
        skipAuthRetry: true,
      });
    },
    onMutate: (next: boolean) => {
      applyAccountTabCacheUpdate(
        (cache) => updateCachedCard(cache, card.bank, card.card_no, { excluded: next }),
        'optimistic',
        ownerEpoch,
      );
      return { previous: card.excluded };
    },
    onError: (_error, _next, context) => {
      if (context) {
        applyAccountTabCacheUpdate(
          (cache) => updateCachedCard(
            cache,
            card.bank,
            card.card_no,
            { excluded: context.previous },
          ),
          'rollback',
          ownerEpoch,
        );
      }
    },
    onSuccess: (_result, next) => {
      applyAccountTabCacheUpdate(
        (cache) => updateCachedCard(cache, card.bank, card.card_no, { excluded: next }),
        'confirmed',
        ownerEpoch,
      );
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['cards'] });
      qc.invalidateQueries({ queryKey: ['portfolio', 'summary'] });
      qc.invalidateQueries({ queryKey: ['transactions'] });
      qc.invalidateQueries({ queryKey: ['frontend-dataset'] });
    },
  });

  // Phase 8.2 C: PATCH /cards/{bank}/{card_no}/nickname
  const renameMut = useMutation({
    mutationFn: (newName: string | null) => {
      assertReplicaOwnerEpoch(ownerKey, ownerEpoch);
      return api(`/cards/${card.bank}/${card.card_no}/nickname`, {
        method: 'PATCH',
        body: { nickname_overwrite: newName },
        skipAuthRetry: true,
      });
    },
    onSuccess: (_result, newName) => {
      applyAccountTabCacheUpdate(
        (cache) => updateCachedCard(
          cache,
          card.bank,
          card.card_no,
          { nickname_overwrite: newName },
        ),
        'durable',
        ownerEpoch,
      );
      qc.invalidateQueries({ queryKey: ['cards'] });
      setEditing(false);
    },
  });

  return (
    <View
      className={`flex-row items-stretch ${borderClass} ${excluded ? 'opacity-50' : ''}`}
      testID={`credit-card-${card.card_no}`}
    >
      <Pressable
        className="flex-1 min-w-0 py-3 pl-4 pr-2"
        onPress={() => router.push({
          pathname: '/(tabs)/transactions',
          params: {
            bank: card.bank,
            kind: 'all',
            card_no: card.card_no,
            drilldown: String(Date.now()),
          },
        })}
        testID={`card-detail-${card.card_no}`}
      >
        {/* 行 1: 卡名 (左) + 本期待繳帳單 (右) */}
        <View className="flex-row items-baseline justify-between">
          <View className="flex-row items-center gap-1 flex-1 min-w-0">
            <Text
              className={`text-body ${excluded ? 'text-ink-500 dark:text-ink-500 line-through' : 'text-ink-900 dark:text-ink-50'}`}
              numberOfLines={1}
            >
              {displayName}
            </Text>
          </View>
          <Text
            className={`text-h3 font-semibold font-mono ml-2 ${
              excluded
                ? 'text-ink-400 dark:text-ink-500 line-through'
                : billDue > 0
                  ? 'text-red-600 dark:text-red-400'
                  : 'text-emerald-600 dark:text-emerald-400'
            }`}
            numberOfLines={1}
          >
            {formatCurrency(billDue, 'TWD')}
          </Text>
        </View>
        {/* 行 2: 末四碼 (左) + 到期日/狀態 (右) */}
        <View className="flex-row items-baseline justify-between mt-1">
          <Text className="text-ink-400 dark:text-ink-500 text-micro font-mono" numberOfLines={1}>
            {subtitle}
            {excluded && <Text className="text-ink-400 dark:text-ink-500"> · 未列入</Text>}
          </Text>
          <Text
            className={`text-micro ${
              dueLabel.kind === 'overdue'
                ? 'text-red-600 dark:text-red-400'
                : dueLabel.kind === 'dueSoon'
                  ? 'text-amber-600 dark:text-amber-400'
                  : 'text-ink-400 dark:text-ink-500'
            }`}
            numberOfLines={1}
          >
            {dueLabel.text}
          </Text>
        </View>
      </Pressable>
      {/* Tap-target: 帳單明細 */}
      <Pressable
        onPress={() => router.push(`/(tabs)/cards/${card.bank}/${encodeURIComponent(card.card_no)}`)}
        className="w-8 items-center justify-center active:bg-ink-100 dark:active:bg-ink-800"
        testID={`card-detail-amount-${card.card_no}`}
        accessibilityLabel="開啟帳單明細"
      >
        <ChevronRight size={19} color="#94a3b8" strokeWidth={2.2} />
      </Pressable>
      <Pressable
        onPress={() => setEditing(true)}
        disabled={renameMut.isPending}
        className="w-8 items-center justify-center active:bg-ink-100 dark:active:bg-ink-800"
        testID={`card-rename-${card.card_no}`}
        accessibilityLabel="編輯卡片暱稱"
      >
        <Pencil size={16} color="#94a3b8" strokeWidth={2.2} />
      </Pressable>
      {/* 排除統計 toggle */}
      <Pressable
        onPress={() => toggleMut.mutate(!excluded)}
        disabled={toggleMut.isPending}
        className="w-10 items-center justify-center active:bg-ink-100 dark:active:bg-ink-800"
        testID={`card-toggle-${card.card_no}`}
        accessibilityLabel={excluded ? '納入淨資產統計' : '不納入淨資產統計'}
      >
        {excluded
          ? <EyeOff size={20} color="#94a3b8" strokeWidth={2.2} />
          : <Eye size={20} color="#64748b" strokeWidth={2.2} />}
      </Pressable>

      <RenameModal
        visible={editing}
        title="編輯卡片暱稱"
        rawLabel={rawName}
        currentValue={overwriteName}
        onClose={() => setEditing(false)}
        onSubmit={(v) => renameMut.mutate(v)}
        isSubmitting={renameMut.isPending}
      />
    </View>
  );
}

// ============================================================
// Helpers
// ============================================================

/** Parse YYYY-MM-DD as a local calendar date. */
function parseDateOnly(date: string | null | undefined): Date | null {
  if (!date) return null;
  const m = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})/.exec(date.trim());
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

function formatDueDateLabel(
  dueDate: string | null | undefined,
  billDueAmount: number,
  billStatus?: string | null,
): { text: string; kind: 'none' | 'dueSoon' | 'overdue' | 'normal' } {
  if (billDueAmount <= 0) return { text: '無需繳款', kind: 'none' };
  // Phase 9.4: backend 是 bill-status 唯一口徑；有 statement_close_date 時，
  // payment 必須不早於該結帳日才算本期已繳，缺結帳日才用 due-30 fallback。
  // paid 代表本期帳單已有真實繳款事實，列表不應再顯示逾期提醒。
  if (billStatus === 'paid') {
    return { text: '無需繳款', kind: 'none' };
  }
  const due = parseDateOnly(dueDate);
  if (!due) return { text: '待繳款', kind: 'normal' };
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const dueDay = new Date(due);
  dueDay.setHours(0, 0, 0, 0);
  const diffDays = Math.round((dueDay.getTime() - today.getTime()) / 86_400_000);
  if (diffDays < 0) return { text: `逾期 ${Math.abs(diffDays)} 天`, kind: 'overdue' };
  if (diffDays === 0) return { text: '今天到期', kind: 'dueSoon' };
  if (diffDays <= 7) return { text: `${diffDays} 天後到期`, kind: 'dueSoon' };
  return {
    text: `${dueDay.getMonth() + 1}/${dueDay.getDate()} 到期`,
    kind: 'normal',
  };
}
