/**
 * 帳戶 Tab — MoneyBook 風 (Phase 6 → 對標使用者 2026-06-14 screenshot).
 *
 * 結構: 每家銀行 group, 內列「子帳戶」+「信用卡」,
 *   - 子帳戶 (新 endpoint /portfolio/accounts): nickname + currency 餘額 + 同步時間
 *   - 信用卡 (/cards): 卡名 + 末四碼
 *
 * Phase 8.2 C (2026-06-14): 三層 nickname 都可編輯 (✏️ icon → inline rename modal)
 *   - bank_accounts.label (跨銀行群組名): PUT /accounts/{id}
 *   - accounts.nickname_overwrite (per-bank 帳戶名): PATCH /portfolio/accounts/{bank}/{no}/nickname
 *   - cards.nickname_overwrite (per-bank 卡片名): PATCH /cards/{bank}/{no}/nickname
 *
 *   鐵則 (對齊 transactions.description_overwrite):
 *     raw label/name/nickname 不動, 只在新欄 *_overwrite 寫覆寫值;
 *     UI 顯示 fallback overwrite || raw; ↺ 重設按鈕清成 NULL.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useRouter } from 'expo-router';
import { useEffect, useMemo, useRef, useState } from 'react';
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
import { api, ApiError, formatApiError } from '@/lib/api';
import { bankMeta } from '@/lib/banks';
import { formatRelativeTime } from '@/lib/datetime';
import { formatDecimalFixed } from '@/lib/decimal';
import { maskCardNo } from '@/lib/mask';
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
  const qc = useQueryClient();

  // Sync infrastructure (Phase 8.6 — 從 dashboard 搬過來 + 改 moneybook 風)
  // - jobsQ: 監聽 /sync/jobs 輪詢 (有 running 時 2s,否則停)
  // - triggerSync: 單一 account_id sync (per-bank ☁️ icon 觸發)
  // - triggerSyncAll: 全部同步 (header ☁️ icon 觸發)
  const jobsQ = useQuery<SyncJob[]>({
    queryKey: ['sync', 'jobs'],
    queryFn: () => api<SyncJob[]>('/sync/jobs'),
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
      // running → done: 全部 invalidate
      qc.invalidateQueries({ queryKey: ['transactions'] });
      qc.invalidateQueries({ queryKey: ['frontend-dataset'] });
      qc.invalidateQueries({ queryKey: ['portfolio'] });
      qc.invalidateQueries({ queryKey: ['cards'] });
      qc.invalidateQueries({ queryKey: ['accounts'] });
      qc.invalidateQueries({ queryKey: ['auto-debit', 'reminders'] });
    }
    prevHasRunningRef.current = hasRunning;
  }, [hasRunning, qc]);

  const triggerSync = useMutation<TriggerSyncResponse, ApiError, number>({
    mutationFn: (accountId) =>
      api<TriggerSyncResponse>(`/sync/account/${accountId}`, {
        method: 'POST',
        body: { headless: true },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['sync', 'jobs'] });
    },
  });

  const triggerSyncAll = useMutation<
    {
      queued: number;
      skipped: number;
    },
    ApiError,
    void
  >({
    mutationFn: () => api('/sync/all', { method: 'POST', body: { headless: true } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['sync', 'jobs'] });
      qc.invalidateQueries({ queryKey: ['portfolio', 'accounts'] });
      qc.invalidateQueries({ queryKey: ['portfolio', 'cards'] });
      qc.invalidateQueries({ queryKey: ['portfolio', 'summary'] });
      qc.invalidateQueries({ queryKey: ['transactions', 'stats'] });
    },
  });

  // /portfolio/accounts — 真實帳戶餘額 (每帳戶 1 row, sync 後才有)
  const balancesQ = useQuery<BankAccountBalance[], ApiError>({
    queryKey: ['portfolio', 'accounts'],
    queryFn: () => api<BankAccountBalance[]>('/portfolio/accounts'),
  });

  // /accounts — 已建立的 cred 槽位 (即使還沒 sync 也有 row)
  // 為了解決「使用者剛新增帳號後到帳戶 tab 看到空白」的反直覺，需要把
  // 「已建但未同步」的銀行也顯示出來。差集邏輯在下方 groupMap 組裝時做。
  const bankAccountsQ = useQuery<BankAccount[], ApiError>({
    queryKey: ['accounts'],
    queryFn: () => api<BankAccount[]>('/accounts'),
  });

  const cardsQ = useQuery<Card[], ApiError>({
    queryKey: ['cards'],
    queryFn: () => api<Card[]>('/cards'),
    retry: false,
  });

  const manualAccountsQ = useQuery<FinancialAccount[], ApiError>({
    queryKey: ['financial-accounts', 'manual'],
    queryFn: () => api<FinancialAccount[]>('/financial-accounts?source=manual'),
  });

  const balances = balancesQ.data ?? [];
  const cards = cardsQ.data ?? [];
  const bankAccounts = bankAccountsQ.data ?? [];

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

  const isLoading = balancesQ.isLoading || cardsQ.isLoading || bankAccountsQ.isLoading;

  // === Sync helpers (Phase 8.6) ===
  // 1. lastJobByBank: 該銀行最近一筆 sync job (任一 account)
  //    bank 粒度而非 account_id 粒度,因為 BankGroupCard ☁️ 是 per-bank icon
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

  // bankToAccountIds: 同銀行的所有 account_id (per-bank ☁️ 觸發時要逐個 trigger)
  const bankToAccountIds = useMemo(() => {
    const m: Record<string, number[]> = {};
    for (const a of bankAccountsQ.data ?? []) {
      if (!m[a.bank]) m[a.bank] = [];
      m[a.bank].push(a.id);
    }
    return m;
  }, [bankAccountsQ.data]);

  // 2. hasRunningJob: 任何 job queued/running → 全部同步按鈕變 disabled + 標示
  const hasRunningJob = useMemo(() => {
    return (jobsQ.data ?? []).some((j) => j.status === 'queued' || j.status === 'running');
  }, [jobsQ.data]);

  // 3. allSyncBusy: 全部同步按鈕的 disabled 條件 (mutation pending 或已有 job 在跑)
  const allSyncBusy = triggerSyncAll.isPending || hasRunningJob;

  // 4. triggerBankSync: per-bank ☁️ 處理. 對該銀行所有 account_id 平行觸發 sync.
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
    <ScrollView className="flex-1 bg-ink-50 dark:bg-ink-950">
      <View className="px-4 py-6 max-w-[800px] w-full mx-auto">
        {/* Header — title + ☁️ 全部同步 (Phase 8.6 — sync 主入口從 dashboard 搬過來) */}
        <View className="flex-row items-start mb-1">
          <View className="flex-1">
            <Text className="text-ink-900 dark:text-ink-50 text-h1 mb-1">帳戶</Text>
          </View>
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
                  <Text className="text-white text-small font-semibold">啟動中...</Text>
                </View>
              ) : hasRunningJob ? (
                <View className="flex-row items-center gap-2">
                  <ActivityIndicator color="#fff" size="small" />
                  <Text className="text-white text-small font-semibold">同步中...</Text>
                </View>
              ) : (
                <Text className="text-white text-small font-semibold">☁️ 全部同步</Text>
              )}
            </Pressable>
          )}
        </View>
        <Text className="text-ink-500 dark:text-ink-400 text-small mb-6">
          管理銀行帳戶、信用卡與已連結券商。銀行資料可用「☁️」同步，券商資料列於下方。
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

        {balancesQ.isError && (
          <ErrorBanner title="讀取帳戶餘額失敗" error={balancesQ.error} />
        )}
        {cardsQ.isError && (
          <ErrorBanner title="讀取信用卡失敗" error={cardsQ.error} />
        )}
        {manualAccountsQ.isError && (
          <ErrorBanner title="讀取手動帳戶失敗" error={manualAccountsQ.error} />
        )}

        {isLoading ? (
          <View className="bg-white dark:bg-ink-900 rounded-2xl p-6 shadow-card">
            <ActivityIndicator />
          </View>
        ) : groups.length === 0 ? (
          <View className="bg-white dark:bg-ink-900 rounded-2xl p-8 items-center shadow-card">
            <Text className="text-ink-400 dark:text-ink-500 text-h3 mb-1">還沒任何銀行帳戶資料</Text>
            <Text className="text-ink-500 dark:text-ink-400 text-small text-center mb-4">
              新增一個銀行帳號後點「☁️ 全部同步」抓帳
            </Text>
            {/* Phase 8.2: 空狀態時的 primary CTA */}
            <Pressable
              onPress={() => router.push('/(tabs)/cards/new')}
              className="bg-brand-600 active:bg-brand-700 rounded-xl px-5 py-3 items-center shadow-brand"
              testID="empty-add-account-btn"
            >
              <Text className="text-white text-h3">+ 新增銀行帳號</Text>
            </Pressable>
          </View>
        ) : (
          <>
            {groups.map((g) => (
              <BankGroupCard
                key={g.bank}
                group={g}
                lastJob={lastJobByBank[g.bank]}
                onSync={() => triggerBankSync(g.bank)}
                syncBusy={syncingBanks.has(g.bank)}
              />
            ))}
            {/* Phase 8.2: 頁底 CTA — 補加新銀行 / 同銀行多帳號 */}
            <Pressable
              onPress={() => router.push('/(tabs)/cards/new')}
              className="bg-white dark:bg-ink-900 rounded-2xl p-5 mt-2 mb-2 items-center border border-dashed border-ink-300 dark:border-ink-700 active:bg-ink-100 dark:active:bg-ink-800"
              testID="add-account-cta"
            >
              <Text className="text-brand-600 dark:text-brand-400 text-h3 font-semibold">
                + 新增銀行帳號
              </Text>
              <Text className="text-ink-500 dark:text-ink-400 text-micro mt-1">
                同一家銀行可建多帳號 (主帳 / 老婆 / 公司)
              </Text>
            </Pressable>
          </>
        )}
        <ManualAccountsSection
          accounts={manualAccountsQ.data ?? []}
          isLoading={manualAccountsQ.isLoading}
        />
        <SnapTradeAccountsSection />
      </View>
    </ScrollView>
  );
}

function ManualAccountsSection({
  accounts,
  isLoading,
}: {
  accounts: FinancialAccount[];
  isLoading: boolean;
}) {
  const router = useRouter();
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
      <View className="flex-row items-center justify-between mb-3">
        <View>
          <Text className="text-ink-900 dark:text-ink-50 text-h2">手動帳戶</Text>
          <Text className="text-ink-500 dark:text-ink-400 text-micro mt-0.5">
            存款、貸款與投資持股／交易
          </Text>
        </View>
        <Pressable
          onPress={() => router.push('/(tabs)/cards/manual/new')}
          accessibilityRole="button"
          accessibilityLabel="新增手動帳戶"
          className="bg-brand-600 active:bg-brand-700 rounded-xl px-4 py-2"
          testID="add-manual-account"
        >
          <Text className="text-white text-small font-semibold">＋ 新增</Text>
        </Pressable>
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
        <Pressable
          key={account.id}
          accessibilityRole="button"
          accessibilityLabel={`開啟手動帳戶 ${account.name}`}
          onPress={() => router.push({
            pathname: '/(tabs)/cards/manual/[account_id]',
            params: { account_id: account.id },
          })}
          className={`bg-white dark:bg-ink-900 rounded-2xl px-4 py-3 shadow-card mb-3 active:opacity-80 ${
            account.included_in_net_worth ? '' : 'opacity-50'
          }`}
          testID={`manual-account-${account.id}`}
        >
          <View className="flex-row items-baseline justify-between gap-3">
            <View className="flex-1 min-w-0">
              <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold" numberOfLines={1}>
                {account.name}
              </Text>
              <Text className="text-ink-500 dark:text-ink-400 text-micro mt-0.5" numberOfLines={1}>
                {account.institution_name} · {typeLabel[account.product_type] ?? account.product_type}
              </Text>
            </View>
            <Text className="text-ink-900 dark:text-ink-50 text-small font-semibold">
              {account.currency}{' '}
              {account.balance == null ? '—' : (formatDecimalFixed(account.balance, 2) ?? '—')}
            </Text>
          </View>
        </Pressable>
      ))}
    </View>
  );
}

function ErrorBanner({ title, error }: { title: string; error: unknown }) {
  return (
    <View className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-2xl p-5 mb-4">
      <Text className="text-red-700 dark:text-red-300 text-h3 mb-2">{title}</Text>
      <Text className="text-red-700 dark:text-red-400 text-small">
        {formatApiError(error)}
      </Text>
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
}: {
  group: BankGroup;
  lastJob: SyncJob | undefined;
  onSync: () => void;
  syncBusy: boolean;
}) {
  const bankLabel = BANK_LABELS[group.bank as SupportedBank] ?? group.bank;
  const meta = bankMeta(group.bank);
  const [autoDebitOpen, setAutoDebitOpen] = useState(false);

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
      {/* Bank header — BankBadge + 銀行名 + ☁️ 同步 + ⚙️ 管理登入 (Phase 8.6) */}
      <View
        className="flex-row items-center gap-2 px-4 py-3 border-b border-ink-100 dark:border-ink-800"
        style={{ borderLeftColor: meta.color, borderLeftWidth: 4 }}
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
        {/* ☁️ 同步該銀行 (Phase 8.6 — 取代原本到 dashboard 才能 sync) */}
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
            <Text className="text-h3">{isFailed ? '🔁' : '☁️'}</Text>
          )}
        </Pressable>
        {/* Phase L10 (2026-06-20): 💳 自動扣繳設定 — 只在有信用卡時顯示 */}
        {group.cards.length > 0 && (
          <Pressable
            onPress={() => setAutoDebitOpen(true)}
            className="w-9 h-9 items-center justify-center rounded-full active:bg-ink-100 dark:active:bg-ink-800"
            testID={`bank-auto-debit-${group.bank}`}
            accessibilityLabel={`設定 ${bankLabel} 自動扣繳`}
          >
            <Text className="text-h3">💳</Text>
          </Pressable>
        )}
        {/* Phase 8.2 (2026-06-15 使用者指示 IA 重整 A 路線):
            ⚙️ 按鈕 push 到 cred 編輯, 讓使用者直接從帳戶 tab 改該銀行的登入欄位 */}
        <Link href={`/(tabs)/cards/credentials/${group.bank}`} asChild>
          <Pressable
            className="w-9 h-9 items-center justify-center rounded-full active:bg-ink-100 dark:active:bg-ink-800"
            testID={`bank-creds-${group.bank}`}
            accessibilityLabel={`管理 ${bankLabel} 登入`}
          >
            <Text className="text-h3">⚙️</Text>
          </Pressable>
        </Link>
      </View>

      {/* 子帳戶 list (存款帳戶) */}
      {group.accounts.map((a, idx) => (
        <AccountRow
          key={`a-${a.account_no}-${a.currency}`}
          account={a}
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

      {/* Phase L10: 自動扣繳設定 modal (per-bank, 跨銀行 TWD 戶 picker) */}
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
// 灰底 + 「未同步」徽章 + 「☁️ 初次同步」CTA (Phase 8.6: 不再跳 dashboard,就地觸發)
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
        ? '已設定登入 — 按右側「☁️」初次同步抓帳'
        : '尚未設定登入 — 點上方「⚙️」填寫銀行登入資訊';

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
            <Text className="text-white text-micro font-semibold">☁️ 初次同步</Text>
          )}
        </Pressable>
      )}
    </View>
  );
}

// ============================================================
// 存款帳戶 row (MoneyBook 風 — icon 圖示 + 名稱 + 餘額 + 同步時間)
// Phase 6 (2026-06-14): excluded → 整列 opacity-40 反灰 + 右側切換按鈕
// Phase 8.2 C (2026-06-14): ✏️ 加暱稱編輯 — PATCH /portfolio/accounts/.../nickname
// ============================================================
function AccountRow({
  account,
  isLast,
}: {
  account: BankAccountBalance;
  isLast: boolean;
}) {
  const qc = useQueryClient();
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const borderClass = isLast ? '' : 'border-b border-ink-100 dark:border-ink-800';
  const isTwd = (account.currency || 'TWD').toUpperCase() === 'TWD';
  const balanceText = formatBalance(account.balance, account.currency);
  const hasBalance = account.balance !== null && account.balance !== undefined;
  const excluded = account.excluded === true;

  // 帳戶名稱 fallback: overwrite > nickname > type > account_no
  // nickname_overwrite 只影響名稱內容；row 右側保留獨立 ✏️ affordance，不在名稱前再加標記。
  const rawName = account.nickname || account.type || account.account_no;
  const overwriteName = (account.nickname_overwrite ?? '').trim();
  const displayName = overwriteName.length > 0 ? overwriteName : rawName;
  const subtitle = account.account_no;

  // 同步時間 → 相對時間 (e.g. "19 小時前")
  const syncedLabel = account.snapshot_date
    ? formatRelativeTime(account.snapshot_date)
    : null;

  // === UI 鐵令 brand strip ===
  // accent(綠 TWD) / amber(外幣) / ink(無資料 / 排除)
  const stripColor = excluded
    ? 'bg-ink-300 dark:bg-ink-700'
    : !hasBalance
      ? 'bg-ink-200 dark:bg-ink-700'
      : isTwd
        ? 'bg-accent-500'
        : 'bg-amber-500';

  // PATCH /portfolio/accounts/{bank}/{account_no}/excluded
  // Write-through cache (2026-06-18): UI 先樂觀更新 → 背景寫 server.
  //   - accounts list: 找 row 翻 excluded
  //   - transactions list (所有 filter 變體): 該帳戶 row 全翻 excluded
  //   - aggregate (summary / stats): 算的, optimistic 太複雜 → settle 後 invalidate
  // 失敗時 onError 用 snapshot rollback. user 在弱網下也是「先看到反灰, 失敗才復原」.
  const toggleMut = useMutation({
    mutationFn: (next: boolean) =>
      api(`/portfolio/accounts/${account.bank}/${account.account_no}/excluded`, {
        method: 'PATCH',
        body: { excluded: next },
      }),
    onMutate: async (next: boolean) => {
      // 取消任何 in-flight refetch, 避免覆蓋 optimistic write
      await qc.cancelQueries({ queryKey: ['portfolio', 'accounts'] });
      await qc.cancelQueries({ queryKey: ['transactions'] });

      // Snapshot (for rollback)
      const prevAccounts = qc.getQueryData<BankAccountBalance[]>(['portfolio', 'accounts']);
      const prevTxns = qc.getQueriesData<{ items: { bank: string; account_or_card: string | null; excluded?: boolean }[] }>({
        queryKey: ['transactions'],
      });

      // Write-through: accounts
      if (prevAccounts) {
        qc.setQueryData<BankAccountBalance[]>(
          ['portfolio', 'accounts'],
          prevAccounts.map((a) =>
            a.bank === account.bank && a.account_no === account.account_no
              ? { ...a, excluded: next }
              : a,
          ),
        );
      }

      // Write-through: 所有 transactions list 變體 (各 filter 各一份 cache)
      // account_or_card 為「末四碼」, 比對 account_no 結尾.
      const tail = account.account_no.slice(-4);
      for (const [key, data] of prevTxns) {
        if (!data || !Array.isArray(data.items)) continue;
        qc.setQueryData(key, {
          ...data,
          items: data.items.map((t) =>
            t.bank === account.bank && t.account_or_card === tail
              ? { ...t, excluded: next }
              : t,
          ),
        });
      }

      return { prevAccounts, prevTxns };
    },
    onError: (_err, _next, ctx) => {
      // Rollback
      if (ctx?.prevAccounts) {
        qc.setQueryData(['portfolio', 'accounts'], ctx.prevAccounts);
      }
      if (ctx?.prevTxns) {
        for (const [key, data] of ctx.prevTxns) {
          qc.setQueryData(key, data);
        }
      }
    },
    onSettled: () => {
      // 不管成功失敗, 背景刷 aggregate (summary / stats 是算的不能 optimistic)
      // accounts / transactions 也順手 invalidate 對齊真值 (server 才是 source of truth)
      qc.invalidateQueries({ queryKey: ['portfolio', 'accounts'] });
      qc.invalidateQueries({ queryKey: ['portfolio', 'summary'] });
      qc.invalidateQueries({ queryKey: ['transactions'] });
      qc.invalidateQueries({ queryKey: ['frontend-dataset'] });
    },
  });

  // Phase 8.2 C: PATCH /portfolio/accounts/{bank}/{account_no}/nickname
  const renameMut = useMutation({
    mutationFn: (newName: string | null) =>
      api(`/portfolio/accounts/${account.bank}/${account.account_no}/nickname`, {
        method: 'PATCH',
        body: { nickname_overwrite: newName },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['portfolio', 'accounts'] });
      setEditing(false);
    },
  });

  return (
    <View
      className={`flex-row items-stretch ${borderClass} ${excluded ? 'opacity-50' : ''}`}
      testID={`account-row-${account.account_no}`}
    >
      {/* UI 鐵令: 4px brand strip */}
      <View className={`w-1 ${stripColor}`} />
      <Pressable
        className="flex-1 min-w-0 py-3 pl-3 pr-2"
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
              className={`text-h3 font-semibold font-mono ml-2 ${excluded ? 'text-ink-400 dark:text-ink-500 line-through' : 'text-emerald-600 dark:text-emerald-400'}`}
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
              ≈ NT$ {account.twd_estimate.toLocaleString('zh-TW')}
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
        <Text className="text-ink-400 dark:text-ink-500 text-small">✏️</Text>
      </Pressable>

      {/* 排除統計 toggle */}
      <Pressable
        onPress={() => toggleMut.mutate(!excluded)}
        disabled={toggleMut.isPending}
        className="w-10 items-center justify-center active:bg-ink-100 dark:active:bg-ink-800"
        testID={`account-toggle-${account.account_no}`}
        accessibilityLabel={excluded ? '納入淨資產統計' : '不納入淨資產統計'}
      >
        <Text className="text-h3">{excluded ? '🙈' : '👁️'}</Text>
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
//   - 左 4px brand strip (red=逾期 / amber=即將到期 / brand=正常)
//   - 廢除 px-4 換成 strip + flex-1, 整體仍 py-3
// Phase 6 (2026-06-14 PM): excluded → 整列反灰 + 卡名劃線 + 右側 toggle
// Phase 8.2 C (2026-06-14): ✏️ 加暱稱編輯 — PATCH /cards/{bank}/{card_no}/nickname
// ============================================================
function CardRow({ card, isLast }: { card: Card; isLast: boolean }) {
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

  const primaryAmount = card.used_credit !== null && card.used_credit !== undefined
    ? card.used_credit
    : card.credit_limit;

  // brand strip 顏色: dueLabel.kind 決定
  const stripColor = excluded
    ? 'bg-ink-300 dark:bg-ink-700'
    : dueLabel.kind === 'overdue'
      ? 'bg-red-500'
      : dueLabel.kind === 'dueSoon'
        ? 'bg-amber-500'
        : 'bg-brand-500';

  // Phase 8.2 C: 顯示 fallback overwrite || raw；row 右側保留獨立 ✏️ affordance，不在卡名/帳戶名前再加標記。
  const rawName = card.name ?? '(未命名)';
  const overwriteName = (card.nickname_overwrite ?? '').trim();
  const displayName = overwriteName.length > 0 ? overwriteName : rawName;

  // PATCH /cards/{bank}/{card_no}/excluded
  // Write-through cache (2026-06-18): UI 先樂觀更新 → 背景寫 server.
  //   - cards list: 找 card 翻 excluded
  //   - transactions list (所有 filter 變體): 該卡 row 全翻 excluded
  //   - aggregate (summary): 算的, settle 後 invalidate
  const toggleMut = useMutation({
    mutationFn: (next: boolean) =>
      api(`/cards/${card.bank}/${card.card_no}/excluded`, {
        method: 'PATCH',
        body: { excluded: next },
      }),
    onMutate: async (next: boolean) => {
      await qc.cancelQueries({ queryKey: ['cards'] });
      await qc.cancelQueries({ queryKey: ['transactions'] });

      const prevCards = qc.getQueryData<Card[]>(['cards']);
      const prevTxns = qc.getQueriesData<{ items: { bank: string; account_or_card: string | null; excluded?: boolean }[] }>({
        queryKey: ['transactions'],
      });

      if (prevCards) {
        qc.setQueryData<Card[]>(
          ['cards'],
          prevCards.map((c) =>
            c.bank === card.bank && c.card_no === card.card_no
              ? { ...c, excluded: next }
              : c,
          ),
        );
      }

      // card_no 末四碼 (account_or_card 的格式)
      const tail = card.card_no.slice(-4);
      for (const [key, data] of prevTxns) {
        if (!data || !Array.isArray(data.items)) continue;
        qc.setQueryData(key, {
          ...data,
          items: data.items.map((t) =>
            t.bank === card.bank && t.account_or_card === tail
              ? { ...t, excluded: next }
              : t,
          ),
        });
      }

      return { prevCards, prevTxns };
    },
    onError: (_err, _next, ctx) => {
      if (ctx?.prevCards) {
        qc.setQueryData(['cards'], ctx.prevCards);
      }
      if (ctx?.prevTxns) {
        for (const [key, data] of ctx.prevTxns) {
          qc.setQueryData(key, data);
        }
      }
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
    mutationFn: (newName: string | null) =>
      api(`/cards/${card.bank}/${card.card_no}/nickname`, {
        method: 'PATCH',
        body: { nickname_overwrite: newName },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['cards'] });
      setEditing(false);
    },
  });

  return (
    <View
      className={`flex-row items-stretch ${borderClass} ${excluded ? 'opacity-50' : ''}`}
      testID={`credit-card-${card.card_no}`}
    >
      {/* UI 鐵令: 4px brand strip (dynamic color) */}
      <View className={`w-1 ${stripColor}`} />
      <Pressable
        className="flex-1 min-w-0 py-3 pl-3 pr-2"
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
        {/* 行 1: 卡名 (左) + 已用額度/信用額度 (右) */}
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
                : card.used_credit !== null && card.used_credit !== undefined && card.used_credit > 0
                  ? 'text-red-600 dark:text-red-400'
                  : 'text-emerald-600 dark:text-emerald-400'
            }`}
            numberOfLines={1}
          >
            {formatTwdAmount(primaryAmount)}
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
        <Text className="text-ink-400 dark:text-ink-500 text-h3">›</Text>
      </Pressable>
      <Pressable
        onPress={() => setEditing(true)}
        disabled={renameMut.isPending}
        className="w-8 items-center justify-center active:bg-ink-100 dark:active:bg-ink-800"
        testID={`card-rename-${card.card_no}`}
        accessibilityLabel="編輯卡片暱稱"
      >
        <Text className="text-ink-400 dark:text-ink-500 text-small">✏️</Text>
      </Pressable>
      {/* 排除統計 toggle */}
      <Pressable
        onPress={() => toggleMut.mutate(!excluded)}
        disabled={toggleMut.isPending}
        className="w-10 items-center justify-center active:bg-ink-100 dark:active:bg-ink-800"
        testID={`card-toggle-${card.card_no}`}
        accessibilityLabel={excluded ? '納入淨資產統計' : '不納入淨資產統計'}
      >
        <Text className="text-h3">{excluded ? '🙈' : '👁️'}</Text>
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

/** 格式化餘額 — 原幣顯示 (TWD: $1,088,682 / JPY: JPY 1,201,387). */
function formatBalance(amount: number | null | undefined, currency: string): string {
  if (amount === null || amount === undefined) return '—';
  const cur = (currency || 'TWD').toUpperCase();
  const formatted = amount.toLocaleString('zh-TW');
  if (cur === 'TWD') return `$ ${formatted}`;
  return `${cur} ${formatted}`;
}

function formatTwdAmount(amount: number | null | undefined): string {
  if (amount === null || amount === undefined) return '—';
  return `$ ${Math.round(amount).toLocaleString('zh-TW')}`;
}

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
