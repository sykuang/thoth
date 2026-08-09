/**
 * 交易記錄 (Phase 5 — /transactions 跨銀行聚合 + 分頁 + filter).
 *
 * Backend partitions are persisted as an owner-scoped local replica (SQLite on
 * native, IndexedDB on Web/Tauri); bank/account/card/period/category/search/
 * direction filters all run against the local projection.
 *
 * UX:
 *   - 明細 header 的篩選按鈕開啟 bank/category/subcategory/search sheet；月份獨立控制
 *   - 中段 stats banner: total + by_bank breakdown
 *   - 主表: 日期/銀行/帳號or卡號/說明/金額/分類
 *   - 分頁: 每頁 100 筆, 上下一頁 + 跳轉
 */
import { useLocalSearchParams } from 'expo-router';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  ActivityIndicator,
  Modal,
  Platform,
  Pressable,
  RefreshControl,
  ScrollView,
  Text,
  TextInput,
  View,
} from 'react-native';
import { KeyboardAwareScrollView } from '@/components/KeyboardAwareScrollView';
import { BulkEditSheet, type BulkTarget } from '@/components/BulkEditSheet';

import { useBreakpoint } from '@/hooks/useBreakpoint';
import { useFrontendDatasetCache } from '@/hooks/useFrontendDatasetCache';
import { usePreferences } from '@/hooks/usePreferences';
import { MonthCarousel } from '@/components/transactions/MonthCarousel';
import { BrokerageTxnRow } from '@/components/transactions/BrokerageTxnRow';
import { TxnRow } from '@/components/transactions/TxnRow';
import { TxnDetailModal } from '@/components/transactions/TxnDetailModal';
import {
  type Granularity,
  currentPeriodKey,
  periodRange,
  periodDisplayLabel,
} from '@/lib/period';
import { api, formatApiError } from '@/lib/api';
import { categorySortRank, sortCategoryKeys } from '@/lib/category-color';
import { mergeTransactionTimeline, transactionDateForBasis } from '@/lib/transactionTimeline';
import {
  applyTxnFilters,
  aggregateByCategory,
  aggregateBySubcategory,
  computePeriodStats,
  txnCashflowAmount,
} from '@/lib/txnFilter';
import {
  type SupportedBank,
  type BankAccount,
  type SnapTradePortfolio,
  type Transaction,
  BANK_LABELS,
} from '@/types/api';

// Deposit transaction crawler coverage gap. These banks currently sync account
// balances but do not write twd_transactions rows, so account drilldown would
// otherwise look like a broken empty state.
const TWD_TXN_UNSUPPORTED_BANKS: ReadonlySet<string> = new Set();

// W (2026-06-17): 砍 KIND_OPTIONS / KIND_BADGE / KIND_LABEL — 全 unused
// (Phase 5 早期殘留, UI 改成 selectionMode 後不再用 kind dropdown).
// SCOPE_LABEL / parseDateForMobileLayout / getDisplayDescription 已抽到
// @/lib/txnDisplay (W Phase 17 2026-06-17), 被 TxnRow + TxnDetailModal 共用.

// Phase 7.5 (2026-06-15 使用者指示) + 2026-07-08 再確認: TXN_TYPE_LABEL/BADGE 全砍。
// 既有 category/subcategory 已覆蓋語意；還款/退款/手續費這種 txn_type tag 多餘。
// txn_type 仍寫進 DB 給 stats endpoint / renderAmount direction 用, 但交易 UI 不顯示。

// fmtAmount() 已搬到 lib/currency.ts 的 renderAmount(), 此處不再重複。
// 主表/detail/手機卡片都改用 renderAmount(txn, fxMode) 取得 primary + sub.

// ============================================================
// Phase 6 Plan C — 月份 carousel helpers
// ============================================================
// Phase 6 (2026-06-14 PM): 升級成 period (granularity = 'day' | 'month' | 'year')
//   - day key = 'YYYY-MM-DD', range = since=until=key 該天
//   - month key = 'YYYY-MM', range = 該月 1 號 ~ 月底
//   - year key = 'YYYY', range = 該年 01-01 ~ 12-31
// 砍掉「本月/全期」兩按鈕, 全期改用「年」granularity 看 (or 多級往上)
// ============================================================
// Granularity 與 period helpers 已抽到 @/lib/period (Phase W refactor 2026-06-17)
// 為什麼: dashboard/reports 也要用，避免 logic drift, 純函式方便 test
// ============================================================

// W Phase 17 (2026-06-17): TxnRow / TxnDetailModal / DetailRow 拆 component 檔
// - @/lib/txnDisplay      — parseDateForMobileLayout / getDisplayDescription / SCOPE_LABEL
// - @/components/transactions/TxnRow.tsx       — memoized row (custom areEqual)
// - @/components/transactions/TxnDetailModal.tsx — modal + DetailRow (single file)
// 原本 transactions.tsx 1531 行 → 拆完約 750 行, 主檔只剩 TransactionsScreen.

export default function TransactionsScreen() {
  const params = useLocalSearchParams<{ bank?: string; kind?: string; account_no?: string; card_no?: string; drilldown?: string }>();
  const initialBank = typeof params.bank === 'string' ? params.bank : '';
  const accountNo = typeof params.account_no === 'string' ? params.account_no : '';
  const cardNo = typeof params.card_no === 'string' ? params.card_no : '';
  const bp = useBreakpoint();
  const datasetQ = useFrontendDatasetCache();
  const preferencesQ = usePreferences();
  const prefs = preferencesQ.hasServerData
    ? preferencesQ.data
    : (datasetQ.data?.preferences ?? preferencesQ.data);
  const fxMode = prefs.fx_display_mode;
  const cardDateBasis = prefs.card_date_basis ?? 'consume';
  const [selectedBanks, setSelectedBanks] = useState<string[]>(initialBank ? [initialBank] : []);
  const [activeAccountNo, setActiveAccountNo] = useState(accountNo);
  const [activeCardNo, setActiveCardNo] = useState(cardNo);
  // Phase 8 (2026-06-15 使用者指示): KIND filter (台幣/已出帳/未出帳) 已從 UI 移除。
  // 2026-07-02: 帳戶/卡片 drilldown 仍可帶 params.kind 給 row identity/歷史相容，
  // 但明細頁不能把沒有 UI 的 kind 當 hidden filter；否則取消銀行 chip 後還要按「清除」才看得到其他 kind。
  // Phase 8 (2026-06-15): 新「分類」filter — 對應 transactions.category 欄, backend 已支援 category= query
  const [category, setCategory] = useState<string>('');  // '' = 全部
  // Phase 8.1 (2026-06-15): 子分類 drill-down — 主類選定後可進一步篩
  const [subcategory, setSubcategory] = useState<string>('');  // '' = 該主類全部
  const [search, setSearch] = useState('');
  // Phase 9 C-2 (2026-06-19): 拔 page state — client-side filter 後一個 period
  // 50-200 筆全 render scroll 順, 不需要分頁. period 內超 5000 才會 truncate
  // (極端 case, UI 警示「資料超量」). 詳 monthStats useMemo 註解.
  // Phase 6 Plan C → Phase 6 (2026-06-14 PM): period carousel
  // granularity = 'day' | 'month' | 'year', period = 該 granularity 的 key
  // 預設 'month' + 本月
  const [granularity, setGranularity] = useState<Granularity>('month');
  const [selectedPeriod, setSelectedPeriod] = useState<string>(currentPeriodKey('month'));
  // Phase 6 (2026-06-14 PM) — 收支表 direction filter (點卡片 toggle)
  const [direction, setDirection] = useState<'all' | 'income' | 'expense'>('all');
  // 2026-06-20 (使用者指示): 重新加回「明細 / 分類」雙 view segmented toggle.
  // - 'list'     : flat list (按日期排序, 即現有行為)
  // - 'category' : 按主類 group, 每組 header 顯示「分類名 · N 筆 · 小計」, group 內全展開
  // 字面只要 group, 不做 collapse (過度設計鐵令); 使用者要 collapse 再加.
  const [viewMode, setViewMode] = useState<'list' | 'category'>('list');
  // L8.5 — detail modal state
  const [detailTxn, setDetailTxn] = useState<Transaction | null>(null);

  // Phase 9.2 (2026-06-17) — bulk edit selection mode
  // selectionMode=true: row 點擊變 toggle select (不開 detail), 顯示底部操作 bar
  // selectedKeys: Set<"bank|kind|id"> 唯一識別 (一筆交易 = bank + kind + raw.id)
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [bulkSheetOpen, setBulkSheetOpen] = useState(false);
  const [filterOpen, setFilterOpen] = useState(false);
  const appliedRouteSignatureRef = useRef<string | null>(null);
  // Expo Router tabs keep this screen mounted; route params are external state.
  // This effect is the intentional bridge from router state into clearable UI filter state.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    const routeSignature = [initialBank, accountNo, cardNo, params.drilldown ?? ''].join('|');
    if (appliedRouteSignatureRef.current === routeSignature) return;
    appliedRouteSignatureRef.current = routeSignature;
    setSelectedBanks(initialBank ? [initialBank] : []);
    setActiveAccountNo(accountNo);
    setActiveCardNo(cardNo);
    if (accountNo || cardNo || typeof params.drilldown === 'string') {
      setCategory('');
      setSubcategory('');
      setSearch('');
      setDirection('all');
      setViewMode('list');
      // Drilldown from Accounts/Cards should preserve the normal default scope: current month.
      // If the bank has no synced rows for this account, show an honest empty state instead of widening to year.
      setGranularity('month');
      setSelectedPeriod(currentPeriodKey('month'));
      setDetailTxn(null);
      setSelectionMode(false);
      setSelectedKeys(new Set());
      setBulkSheetOpen(false);
      setFilterOpen(false);
    }
  }, [initialBank, accountNo, cardNo, params.drilldown]);
  /* eslint-enable react-hooks/set-state-in-effect */
  // 統一 row identity：txnKey 與 row key 共用 t.id (Transaction type 已標 required)
  const txnKey = (t: Transaction) => `${t.bank}|${t.kind}|${t.id}`;
  function toggleSelect(t: Transaction) {
    const k = txnKey(t);
    setSelectedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  }
  function exitSelectionMode() {
    setSelectionMode(false);
    setSelectedKeys(new Set());
  }

  const bankAccountsQ = useQuery<BankAccount[]>({
    queryKey: ['accounts'],
    queryFn: () => api<BankAccount[]>('/accounts'),
  });

  // Transactions come from the local replica; account/card read models remain on
  // their existing API paths until those derived projections are migrated fully.
  const availableBanks = useMemo(() => {
    const banks = new Set<string>();
    for (const a of bankAccountsQ.data ?? []) {
      if (a.has_creds) banks.add(String(a.bank));
    }
    for (const t of datasetQ.data?.transactions ?? []) banks.add(t.bank);
    if (initialBank) banks.add(initialBank);
    return Array.from(banks);
  }, [bankAccountsQ.data, datasetQ.data, initialBank]);

  const drilldownScopeActive = Boolean(
    initialBank && selectedBanks.length === 1 && selectedBanks[0] === initialBank,
  );
  const effectiveAccountNo = drilldownScopeActive ? activeAccountNo : '';
  const effectiveCardNo = drilldownScopeActive ? activeCardNo : '';
  const brokerageScopeActive = selectedBanks.length === 0 && !effectiveAccountNo && !effectiveCardNo;
  const brokerageQ = useQuery({
    queryKey: ['snaptrade', 'portfolio'],
    queryFn: () => api<SnapTradePortfolio>('/snaptrade/portfolio'),
    enabled: brokerageScopeActive,
  });
  const activeBrokeragePortfolio = brokerageScopeActive ? brokerageQ.data : undefined;

  const rawItems = useMemo(() => {
    const { since, until } = periodRange(granularity, selectedPeriod);
    let items = datasetQ.data?.transactions ?? [];
    if (selectedBanks.length > 0) items = items.filter((t) => selectedBanks.includes(t.bank));
    if (effectiveAccountNo) items = items.filter((t) => t.account_no === effectiveAccountNo);
    if (effectiveCardNo) items = items.filter((t) => t.card_no === effectiveCardNo);
    items = items.filter((t) => {
      const d = transactionDateForBasis(t, cardDateBasis);
      return d >= since && d <= until;
    });
    return items;
  }, [datasetQ.data, selectedBanks, effectiveAccountNo, effectiveCardNo, granularity, selectedPeriod, cardDateBasis]);

  const transactionRefreshing = (
    (datasetQ.isRefetching && !datasetQ.isLoading)
    || datasetQ.isRefreshingChanges
    || (brokerageScopeActive && brokerageQ.isRefetching)
  );

  // chip 來源 (主類 chip) — 不被 category/subcategory/direction/search filter 影響,
  // 一律從 rawItems aggregate. 對齊 backend transactions.py:656-661 邏輯
  // (skip excluded/auto_excluded, NULL 用 '__null__' sentinel).
  const byCategory = useMemo(() => aggregateByCategory(rawItems), [rawItems]);
  const categoryKeys = useMemo(() => sortCategoryKeys(Object.keys(byCategory)), [byCategory]);

  // 子類 chip — 限縮到當前主類 (對齊 backend transactions.py:662-666).
  // category='' 或 '__null__' 時不顯示子類 chip (回 {}).
  const bySubcategory = useMemo(
    () => aggregateBySubcategory(rawItems, category),
    [rawItems, category],
  );

  // 實際 render 的 list (套全部 filter, 包含 search/category/subcategory/direction).
  // 2026-06-22 (使用者指示): 分類 view 強制不套 client-side filter — 永遠顯示「整個 period 的分類分佈」.
  //   - filter card 已隨 viewMode 隱藏 (見下方 JSX), 但 state (category/subcategory/direction/search)
  //     仍可能殘留 (例如先在 list view 點了 chip, 再切 category view).
  //   - 這裡用 viewMode 短路: 分類 view 直接吃 rawItems, list view 才套 filter.
  //   - 為何不直接清 state? 因為使用者「列 → 分類 → 列回去」要保留先前篩選, 才符合 muscle memory.
  const filteredItems = useMemo(
    () => {
      const base = rawItems;
      return viewMode === 'category'
        ? base
        : applyTxnFilters(base, { category, subcategory, direction, search });
    },
    [viewMode, rawItems, category, subcategory, direction, search],
  );

  const brokeragePeriodActivities = useMemo(() => {
    if (!brokerageScopeActive) return [];
    const { since, until } = periodRange(granularity, selectedPeriod);
    return (activeBrokeragePortfolio?.activities ?? []).filter((activity) => {
      const date = (activity.trade_date ?? activity.settlement_date ?? '').slice(0, 10);
      return date >= since && date <= until;
    });
  }, [activeBrokeragePortfolio, brokerageScopeActive, granularity, selectedPeriod]);

  const visibleBrokerageActivities = useMemo(() => {
    if (viewMode !== 'list' || category || subcategory || direction !== 'all' || selectionMode) return [];
    const needle = search.trim().toLowerCase();
    if (!needle) return brokeragePeriodActivities;
    const accounts = new Map((activeBrokeragePortfolio?.accounts ?? []).map((account) => [account.id, account]));
    return brokeragePeriodActivities.filter((activity) => {
      const account = accounts.get(activity.account_id);
      return [
        activity.type,
        activity.symbol,
        activity.description,
        account?.institution_name,
        account?.name,
      ].some((value) => value?.toLowerCase().includes(needle));
    });
  }, [brokeragePeriodActivities, activeBrokeragePortfolio, viewMode, category, subcategory, direction, search, selectionMode]);

  const timelineItems = useMemo(
    () => mergeTransactionTimeline(
      filteredItems,
      visibleBrokerageActivities,
      activeBrokeragePortfolio?.accounts ?? [],
      cardDateBasis,
    ),
    [filteredItems, visibleBrokerageActivities, activeBrokeragePortfolio, cardDateBasis],
  );

  // 收支表上方兩張卡 (收入/支出) — 數字應代表「目前 period + 非方向 filter」的總覽。
  // 方向 toggle（收入/支出/全部）只是 list 篩選，不應回頭改變卡片自身數字；否則點「收入」後
  // 支出卡變 0、點「支出」後收入卡變 0，使用者看到的收入數字會在三種 toggle 間跳動。
  // 仍保留 category/subcategory/search/bank/kind/period 對卡片的影響，因為那些是資料範圍 filter。
  const statsItems = useMemo(
    () => {
      const base = rawItems;
      return viewMode === 'category'
        ? base
        : applyTxnFilters(base, { category, subcategory, direction: 'all', search });
    },
    [viewMode, rawItems, category, subcategory, search],
  );
  const monthStats = useMemo(() => computePeriodStats(statsItems), [statsItems]);

  // 2026-06-20 (使用者指示 v2): 「分類」viewMode 改 aggregate bar chart 列.
  // 鐵則:
  //   - 不展開 row, 只顯示「分類名 · 金額 · % progress bar」單行
  //   - skip excluded / auto_excluded (跟 backend by_category 一致, transactions.py:656-661)
  //   - pct = |subtotal| / Σ|subtotal| × 100 (絕對值占比, direction filter 已先套過,
  //     所以 direction='expense' 時 filteredItems 已只剩 expense row, pct 自然只在
  //     支出範圍內算 → 不需另寫 direction-aware 分支)
  //   - 2026-07-05 A 方案: 排序改固定生活記帳順序，不按 pct；否則「飲食」仍可能
  //     在分類 view 跑到底，跟上方 category chips 修法不一致。
  const groupedByCategory = useMemo(() => {
    type Group = { key: string; label: string; subtotal: number; count: number; pct: number };
    const map = new Map<string, Omit<Group, 'pct'>>();
    for (const t of filteredItems) {
      if (t.excluded === true || t.auto_excluded === true) continue;
      const key = t.category || '__null__';
      const g = map.get(key) ?? { key, label: key === '__null__' ? '未分類' : key, subtotal: 0, count: 0 };
      g.subtotal += txnCashflowAmount(t);
      g.count += 1;
      map.set(key, g);
    }
    const groups = Array.from(map.values());
    const total = groups.reduce((s, g) => s + Math.abs(g.subtotal), 0);
    return groups
      .map<Group>((g) => ({ ...g, pct: total > 0 ? (Math.abs(g.subtotal) / total) * 100 : 0 }))
      .sort((a, b) => {
        const rankDiff = categorySortRank(a.key) - categorySortRank(b.key);
        if (rankDiff !== 0) return rankDiff;
        return a.label.localeCompare(b.label, 'zh-Hant');
      });
  }, [filteredItems]);

  function toggleBank(b: string) {
    setActiveAccountNo('');
    setActiveCardNo('');
    setSelectedBanks((prev) =>
      prev.includes(b) ? prev.filter((x) => x !== b) : [...prev, b],
    );
  }

  function clearFilters() {
    setSelectedBanks([]);
    setActiveAccountNo('');
    setActiveCardNo('');
    setCategory('');
    setSubcategory('');
    setSearch('');
  }

  // Phase 9 C-2 (2026-06-19): total 看 filtered (client-side filter 後), 顯示「N 筆」.
  const filteredCount = viewMode === 'list' ? timelineItems.length : filteredItems.length;
  const rawCount = rawItems.length + (viewMode === 'list' ? brokeragePeriodActivities.length : 0);
  const activeFilterCount =
    Number(selectedBanks.length > 0)
    + Number(Boolean(effectiveAccountNo || effectiveCardNo))
    + Number(category !== '')
    + Number(subcategory !== '')
    + Number(search.trim().length > 0);
  const brokerageAccountCount = activeBrokeragePortfolio?.accounts.length ?? 0;
  const isUnsupportedAccountDrilldown = Boolean(
    effectiveAccountNo && selectedBanks.length === 1 && TWD_TXN_UNSUPPORTED_BANKS.has(selectedBanks[0]),
  );

  // Phase 9 C-2 (2026-06-19): 既有 monthStats useMemo (L215-235) 已被上方
  // computePeriodStats(rawItems) 取代. 砍掉 day/year/month 三分支邏輯,
  // 因為全 snapshot 已在 frontend，monthStats 永遠從 rawItems 算就準.
  const incomeAmt = monthStats?.income ?? 0;
  const expenseAmt = Math.abs(monthStats?.expense ?? 0);

  // Period label 給 section header / CategorySummary 用
  const periodLabel = periodDisplayLabel(granularity, selectedPeriod);

  return (
    <>
    <KeyboardAwareScrollView
      className="flex-1 bg-ink-50 dark:bg-ink-950"
      refreshControl={
        <RefreshControl
          refreshing={transactionRefreshing}
          onRefresh={() => {
            void datasetQ.refreshSnapshot();
            if (brokerageScopeActive) void brokerageQ.refetch();
          }}
          tintColor="#7c3aed"
        />
      }
    >
      <View className="px-4 py-4 max-w-[800px] w-full mx-auto">
        {/* Header: 收支表 標題 + Phase 9.2 選取模式按鈕 */}
        <View className="flex-row items-center justify-between mb-3">
          <View className="w-16" />{/* spacer 對稱 */}
          <Text className="text-ink-900 dark:text-ink-50 text-h1 text-center flex-1">收支表</Text>
          <View className="w-16 items-end">
            {selectionMode ? (
              <Pressable
                onPress={exitSelectionMode}
                className="px-3 py-1 rounded-lg active:bg-ink-100 dark:active:bg-ink-800"
                testID="txn-selection-exit"
              >
                <Text className="text-brand-600 dark:text-brand-400 text-small">取消</Text>
              </Pressable>
            ) : (
              <Pressable
                onPress={() => setSelectionMode(true)}
                className="px-3 py-1 rounded-lg active:bg-ink-100 dark:active:bg-ink-800"
                testID="txn-selection-enter"
              >
                <Text className="text-brand-600 dark:text-brand-400 text-small">選取</Text>
              </Pressable>
            )}
          </View>
        </View>

        {/* Phase 6 (2026-06-14 PM) — period carousel + granularity segmented */}
        <MonthCarousel
          granularity={granularity}
          selectedPeriod={selectedPeriod}
          onGranularityChange={(g) => {
            // 切 granularity 時把 period 跳到當下 (避免 'day' key 直接吃 month key)
            setGranularity(g);
            setSelectedPeriod(currentPeriodKey(g));
          }}
          onPeriodChange={(p) => {
            setSelectedPeriod(p);
          }}
          monthStat={monthStats}
        />

        {/* 收入 / 支出 兩張卡 — 對標 MoneyBook, 可點 toggle direction filter */}
        {/* Phase 6 (2026-06-14 PM): 點收入卡 = 只看收入; 點支出卡 = 只看支出;
            再點一次 = 取消回全部. Selected card 加綠/紅邊框 + 粗體 label */}
        <View className="flex-row gap-3 mb-3">
          <Pressable
            onPress={() => {
              setDirection((d) => (d === 'income' ? 'all' : 'income'));
              // Phase 9 C-2: setPage 已砍 (拔分頁)
            }}
            className={`flex-1 bg-white dark:bg-ink-900 rounded-2xl px-4 py-3 active:opacity-80 ${
              direction === 'income'
                ? 'border-2 border-accent-500 shadow-pop'
                : direction === 'expense'
                  ? 'opacity-40 shadow-card'
                  : 'border-2 border-transparent shadow-card'
            }`}
            testID="income-card-toggle"
            accessibilityLabel={direction === 'income' ? '取消只看收入' : '只看收入'}
          >
            <Text
              className={`text-small mb-1 ${
                direction === 'income'
                  ? 'text-accent-600 dark:text-accent-500 font-semibold'
                  : 'text-ink-500 dark:text-ink-400'
              }`}
            >
              收入{direction === 'income' && ' ✓'}
            </Text>
            <Text
              className={`text-h2 font-semibold ${
                direction === 'income'
                  ? 'text-accent-700 dark:text-accent-400'
                  : 'text-ink-700 dark:text-ink-300'
              }`}
              numberOfLines={1}
            >
              $ {incomeAmt.toLocaleString('zh-TW')}
            </Text>
          </Pressable>
          <Pressable
            onPress={() => {
              setDirection((d) => (d === 'expense' ? 'all' : 'expense'));
              // Phase 9 C-2: setPage 已砍 (拔分頁)
            }}
            className={`flex-1 bg-white dark:bg-ink-900 rounded-2xl px-4 py-3 active:opacity-80 ${
              direction === 'expense'
                ? 'border-2 border-red-500 shadow-pop'
                : direction === 'income'
                  ? 'opacity-40 shadow-card'
                  : 'border-2 border-transparent shadow-pop'
            }`}
            testID="expense-card-toggle"
            accessibilityLabel={direction === 'expense' ? '取消只看支出' : '只看支出'}
          >
            <Text
              className={`text-small mb-1 ${
                direction === 'expense'
                  ? 'text-red-600 dark:text-red-400 font-semibold'
                  : 'text-ink-500 dark:text-ink-400'
              }`}
            >
              支出{direction === 'expense' && ' ✓'}
            </Text>
            <Text
              className={`text-h2 font-bold ${
                direction === 'expense'
                  ? 'text-red-700 dark:text-red-400'
                  : 'text-ink-900 dark:text-ink-50'
              }`}
              numberOfLines={1}
            >
              $ {expenseAmt.toLocaleString('zh-TW')}
            </Text>
          </Pressable>
        </View>

        {/* Phase 8 (2026-06-15 使用者指示): 「分類/明細」segmented tab 已移除 — 永遠顯示明細 */}
        {/* (原 CategorySummary block 也一併拆掉; 分類視角改由 dashboard 卡片承擔) */}

        {/* Section header — Phase 6 雙 view 段切回 (2026-06-20 使用者) */}
        <View className="flex-row items-center mb-3 px-1">
          <Text className="text-ink-900 dark:text-ink-50 text-h3">支出明細</Text>
          <Text className="text-ink-400 dark:text-ink-500 text-small ml-2 flex-1">
            {periodLabel}
          </Text>
          {viewMode === 'list' && (
            <Pressable
              onPress={() => setFilterOpen(true)}
              className={`mr-2 px-3 py-1.5 rounded-lg border ${
                activeFilterCount > 0
                  ? 'bg-brand-50 border-brand-300 dark:bg-brand-950 dark:border-brand-700'
                  : 'bg-white border-ink-200 dark:bg-ink-900 dark:border-ink-700'
              }`}
              testID="txn-filter-open"
              accessibilityRole="button"
              accessibilityLabel={activeFilterCount > 0 ? `篩選，已套用 ${activeFilterCount} 項` : '篩選交易'}
            >
              <Text className={`text-small font-semibold ${
                activeFilterCount > 0
                  ? 'text-brand-700 dark:text-brand-300'
                  : 'text-ink-600 dark:text-ink-300'
              }`}>
                篩選{activeFilterCount > 0 ? ` ${activeFilterCount}` : ''}
              </Text>
            </Pressable>
          )}
          {/* 「明細 / 分類」segmented — 對應 viewMode state, 不影響 filter/卡片 */}
          <View className="flex-row bg-ink-100 dark:bg-ink-800 rounded-lg p-0.5">
            <Pressable
              onPress={() => setViewMode('list')}
              className={`px-3 py-1 rounded-md ${
                viewMode === 'list' ? 'bg-white dark:bg-ink-700 shadow-card' : ''
              }`}
              testID="txn-view-list"
              accessibilityLabel="切換到明細視圖"
            >
              <Text
                className={`text-small ${
                  viewMode === 'list'
                    ? 'text-ink-900 dark:text-ink-50 font-semibold'
                    : 'text-ink-500 dark:text-ink-400'
                }`}
              >
                明細
              </Text>
            </Pressable>
            <Pressable
              onPress={() => setViewMode('category')}
              className={`px-3 py-1 rounded-md ${
                viewMode === 'category' ? 'bg-white dark:bg-ink-700 shadow-card' : ''
              }`}
              testID="txn-view-category"
              accessibilityLabel="切換到分類視圖"
            >
              <Text
                className={`text-small ${
                  viewMode === 'category'
                    ? 'text-ink-900 dark:text-ink-50 font-semibold'
                    : 'text-ink-500 dark:text-ink-400'
                }`}
              >
                分類
              </Text>
            </Pressable>
          </View>
        </View>

        {/* Phase 8.2 (2026-06-15 使用者指示): stats banner「N 筆全歷史交易」+
            銀行 chip 列已砍 — 雜訊大於資訊，by_bank 統計也已在 dashboard 銀行 group
            裡分別看得到，這裡再 dump 一份只搶 filter section 的視覺權重。 */}

        {/* ===== Filters ===== */}
        {/* Full filter controls live in the button-triggered sheet below; keep the list compact. */}

        {/* ===== 主表 / 載入 / 錯誤 ===== */}
        {viewMode === 'list' && brokerageScopeActive && brokerageQ.isError && (
          <Text className="text-red-600 dark:text-red-400 text-small mb-3">
            券商交易讀取失敗：{formatApiError(brokerageQ.error)}
          </Text>
        )}
        {datasetQ.isLoading || (brokerageScopeActive && brokerageQ.isLoading) ? (
          <View className="bg-white dark:bg-ink-900 rounded-2xl p-8 items-center shadow-card">
            <ActivityIndicator />
          </View>
        ) : datasetQ.isError ? (
          <View className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-2xl p-5">
            <Text className="text-red-700 dark:text-red-300 text-h3 mb-2">查詢失敗</Text>
            <Text className="text-red-700 dark:text-red-400 text-small">
              {formatApiError(datasetQ.error)}
            </Text>
          </View>
        ) : filteredCount === 0 ? (
          <View className="bg-white dark:bg-ink-900 rounded-2xl p-8 items-center shadow-card">
            <Text className="text-ink-400 dark:text-ink-500 text-h3 mb-1">
              {brokerageScopeActive && brokerageQ.isError
                ? '券商交易目前無法載入'
                : availableBanks.length === 0 && brokerageAccountCount === 0
                  ? '還沒有任何交易來源'
                : isUnsupportedAccountDrilldown
                  ? '此銀行尚未支援存款交易明細同步'
                  : '此篩選沒有任何交易'}
            </Text>
            <Text className="text-ink-500 dark:text-ink-400 text-small text-center">
              {brokerageScopeActive && brokerageQ.isError
                ? '請下拉重新整理'
                : availableBanks.length === 0 && brokerageAccountCount === 0
                  ? '到「帳戶」tab 新增銀行或券商帳戶，同步後這裡就會有資料'
                : isUnsupportedAccountDrilldown
                  ? '目前這家銀行只同步到帳戶餘額，尚未同步存款交易明細；清除篩選也不會出現此帳戶的明細。'
                  : '試試清除篩選或執行同步'}
            </Text>
          </View>
        ) : (
          <>
            {/* Phase 9 C-2 (2026-06-19): page meta 改 client-side filter 計數 */}
            <View className="flex-row items-center justify-between mb-2 px-1">
              <Text className="text-ink-500 dark:text-ink-400 text-small">
                共{' '}
                <Text className="text-ink-900 dark:text-ink-50 font-semibold">
                  {filteredCount.toLocaleString()}
                </Text>{' '}
                筆
                {filteredCount !== rawCount && (
                  <Text className="text-ink-400 dark:text-ink-500">
                    {' '}/ 該期間 {rawCount.toLocaleString()} 筆
                  </Text>
                )}
              </Text>
            </View>

            <View className="bg-white dark:bg-ink-900 rounded-2xl border border-ink-200 dark:border-ink-700 overflow-hidden">
              {/* table header — 桌機才顯示 (僅 list view; category view 用 group header) */}
              {bp.isMd && viewMode === 'list' && (
                <View className="flex-row bg-ink-50 dark:bg-ink-800 border-b border-ink-200 dark:border-ink-700">
                  <Text className="w-28 px-3 py-2 text-micro font-semibold text-ink-600 dark:text-ink-300">
                    日期
                  </Text>
                  <Text className="w-20 px-3 py-2 text-micro font-semibold text-ink-600 dark:text-ink-300">
                    來源
                  </Text>
                  <Text className="w-32 px-3 py-2 text-micro font-semibold text-ink-600 dark:text-ink-300">
                    帳戶
                  </Text>
                  <Text className="flex-1 px-3 py-2 text-micro font-semibold text-ink-600 dark:text-ink-300">
                    說明
                  </Text>
                  <Text className="w-20 px-3 py-2 text-micro font-semibold text-ink-600 dark:text-ink-300">
                    分類
                  </Text>
                  <Text className="w-32 px-3 py-2 text-micro font-semibold text-ink-600 dark:text-ink-300 text-right">
                    金額
                  </Text>
                </View>
              )}

              {/* 2026-06-20 雙 view: list = flat / category = group by 主類 */}
              {viewMode === 'list' ? (
                timelineItems.map((item) => item.source === 'brokerage' ? (
                  <BrokerageTxnRow
                    key={item.key}
                    activity={item.activity}
                    account={item.account}
                    wide={bp.isMd}
                  />
                ) : (
                  <TxnRow
                    key={item.key}
                    t={item.transaction}
                    wide={bp.isMd}
                    fxMode={fxMode}
                    cardDateBasis={cardDateBasis}
                    onPress={() => {
                      if (selectionMode) {
                        toggleSelect(item.transaction);
                      } else {
                        setDetailTxn(item.transaction);
                      }
                    }}
                    onLongPress={() => {
                      if (!selectionMode) {
                        setSelectionMode(true);
                        setSelectedKeys(new Set([txnKey(item.transaction)]));
                      }
                    }}
                    selected={selectionMode && selectedKeys.has(txnKey(item.transaction))}
                    selectionMode={selectionMode}
                  />
                ))
              ) : (
                // 2026-06-20 (使用者指示 v2): 分類視角 = aggregate bar chart 列
                // 每列: 分類名 + 金額(右) → progress bar (跨整個寬度) + % 標籤(右)
                // 按 pct 降序, 大宗在頂
                groupedByCategory.map((g) => {
                  const barColor =
                    g.subtotal > 0
                      ? 'bg-accent-500 dark:bg-accent-500'
                      : g.subtotal < 0
                        ? 'bg-red-500 dark:bg-red-500'
                        : 'bg-ink-400 dark:bg-ink-500';
                  const amountColor =
                    g.subtotal > 0
                      ? 'text-accent-600 dark:text-accent-500'
                      : g.subtotal < 0
                        ? 'text-red-600 dark:text-red-400'
                        : 'text-ink-500 dark:text-ink-400';
                  return (
                    <Pressable
                      key={g.key}
                      onPress={() => {
                        // 2026-06-20 (使用者指示): 點分類列 → 跳明細視角 + 套該分類 filter
                        // 對齊既有主類 chip 邏輯 (L513-515): 換主類時清子類, 避免 stale subcategory
                        if (category !== g.key) setSubcategory('');
                        setCategory(g.key);
                        setViewMode('list');
                      }}
                      className="px-4 py-3 border-b border-ink-100 dark:border-ink-800 active:bg-ink-50 dark:active:bg-ink-800"
                      testID={`txn-cat-row-${g.key}`}
                    >
                      {/* 行 1: 分類名 (左) + 金額 (右) */}
                      <View className="flex-row items-center justify-between mb-1.5">
                        <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold">
                          {g.label}
                        </Text>
                        <Text className={`text-body font-semibold ${amountColor}`}>
                          {g.subtotal > 0 ? '+' : g.subtotal < 0 ? '-' : ''}$
                          {Math.abs(g.subtotal).toLocaleString('zh-TW', { maximumFractionDigits: 0 })}
                        </Text>
                      </View>
                      {/* 行 2: progress bar (跨寬) + % 標籤 (右) */}
                      <View className="flex-row items-center gap-2">
                        <View className="flex-1 h-1.5 bg-ink-100 dark:bg-ink-800 rounded-full overflow-hidden">
                          <View
                            className={`h-full ${barColor} rounded-full`}
                            style={{ width: `${Math.max(g.pct, 1)}%` }}
                          />
                        </View>
                        <Text className="text-ink-500 dark:text-ink-400 text-micro font-semibold w-12 text-right">
                          {g.pct.toFixed(1)}%
                        </Text>
                      </View>
                    </Pressable>
                  );
                })
              )}
            </View>

            {/* Phase 9 C-2 (2026-06-19): pagination 區塊整段砍 — 一個 period 50-200 筆 scroll 順 */}
          </>
        )}
      </View>
    </KeyboardAwareScrollView>
    <Modal
        visible={filterOpen}
        transparent
        animationType={Platform.OS === 'ios' ? 'slide' : 'fade'}
        onRequestClose={() => setFilterOpen(false)}
      >
        <Pressable
          className="flex-1 bg-black/50 justify-end web:items-center web:justify-center web:p-4"
          onPress={() => setFilterOpen(false)}
          accessible={false}
          focusable={false}
        >
          <Pressable
            className="bg-white dark:bg-ink-900 rounded-t-3xl web:rounded-2xl w-full max-w-[640px] max-h-[85%] shadow-pop"
            onPress={(event) => event.stopPropagation()}
            accessible={false}
            focusable={false}
            testID="txn-filter-sheet"
          >
            <View className="flex-row items-center justify-between px-5 pt-5 pb-3 border-b border-ink-100 dark:border-ink-800">
              <Pressable
                onPress={clearFilters}
                disabled={activeFilterCount === 0}
                className={activeFilterCount === 0 ? 'opacity-30' : ''}
                testID="txn-filter-clear"
                accessibilityRole="button"
                accessibilityLabel="清除交易篩選"
              >
                <Text className="text-brand-600 dark:text-brand-400 text-body font-semibold">清除</Text>
              </Pressable>
              <Text className="text-ink-900 dark:text-ink-50 text-h3 font-bold">篩選交易</Text>
              <Pressable
                onPress={() => setFilterOpen(false)}
                testID="txn-filter-done"
                accessibilityRole="button"
                accessibilityLabel="完成交易篩選"
              >
                <Text className="text-brand-600 dark:text-brand-400 text-body font-semibold">完成</Text>
              </Pressable>
            </View>
            <ScrollView
              className="px-5 pt-4"
              keyboardShouldPersistTaps="handled"
              automaticallyAdjustKeyboardInsets
              showsVerticalScrollIndicator={false}
              contentContainerStyle={{ paddingBottom: 36 }}
            >
              {availableBanks.length > 0 && (
                <View className="mb-5">
                  <Text className="text-ink-500 dark:text-ink-400 text-micro font-semibold tracking-wider mb-2">
                    銀行 (不選=全部)
                  </Text>
                  <View className="flex-row flex-wrap gap-2">
                    {availableBanks.map((bank) => {
                      const selected = selectedBanks.includes(bank);
                      return (
                        <Pressable
                          key={bank}
                          onPress={() => toggleBank(bank)}
                          className={`px-3 py-2 rounded-full border ${
                            selected
                              ? 'bg-brand-600 border-brand-600 dark:bg-brand-500 dark:border-brand-500'
                              : 'bg-white border-ink-200 dark:bg-ink-800 dark:border-ink-700'
                          }`}
                          accessibilityRole="button"
                          accessibilityState={{ selected }}
                        >
                          <Text className={`text-small ${
                            selected ? 'text-white font-semibold' : 'text-ink-600 dark:text-ink-300'
                          }`}>
                            {BANK_LABELS[bank as SupportedBank] ?? bank}
                          </Text>
                        </Pressable>
                      );
                    })}
                  </View>
                </View>
              )}

              <View className="mb-5">
                <Text className="text-ink-500 dark:text-ink-400 text-micro font-semibold tracking-wider mb-2">
                  分類
                </Text>
                <View className="flex-row flex-wrap gap-2">
                  <Pressable
                    onPress={() => {
                      setCategory('');
                      setSubcategory('');
                    }}
                    className={`px-3 py-2 rounded-full border ${
                      category === ''
                        ? 'bg-brand-600 border-brand-600 dark:bg-brand-500 dark:border-brand-500'
                        : 'bg-white border-ink-200 dark:bg-ink-800 dark:border-ink-700'
                    }`}
                    accessibilityRole="button"
                    accessibilityState={{ selected: category === '' }}
                  >
                    <Text className={`text-small ${
                      category === '' ? 'text-white font-semibold' : 'text-ink-600 dark:text-ink-300'
                    }`}>
                      全部
                    </Text>
                  </Pressable>
                  {categoryKeys.map((key) => {
                    const selected = category === key;
                    return (
                      <Pressable
                        key={key}
                        onPress={() => {
                          if (!selected) setSubcategory('');
                          setCategory(key);
                        }}
                        className={`px-3 py-2 rounded-full border ${
                          selected
                            ? 'bg-brand-600 border-brand-600 dark:bg-brand-500 dark:border-brand-500'
                            : 'bg-white border-ink-200 dark:bg-ink-800 dark:border-ink-700'
                        }`}
                        accessibilityRole="button"
                        accessibilityState={{ selected }}
                      >
                        <Text className={`text-small ${
                          selected ? 'text-white font-semibold' : 'text-ink-600 dark:text-ink-300'
                        }`}>
                          {key === '__null__' ? '未分類' : key}
                        </Text>
                      </Pressable>
                    );
                  })}
                </View>
              </View>

              {category.length > 0 && Object.keys(bySubcategory).length > 0 && (
                <View className="mb-5">
                  <Text className="text-ink-500 dark:text-ink-400 text-micro font-semibold tracking-wider mb-2">
                    {category} 子分類
                  </Text>
                  <View className="flex-row flex-wrap gap-2">
                    <Pressable
                      onPress={() => setSubcategory('')}
                      className={`px-3 py-2 rounded-full border ${
                        subcategory === ''
                          ? 'bg-brand-600 border-brand-600 dark:bg-brand-500 dark:border-brand-500'
                          : 'bg-white border-ink-200 dark:bg-ink-800 dark:border-ink-700'
                      }`}
                      accessibilityRole="button"
                      accessibilityState={{ selected: subcategory === '' }}
                    >
                      <Text className={`text-small ${
                        subcategory === '' ? 'text-white font-semibold' : 'text-ink-600 dark:text-ink-300'
                      }`}>
                        全部
                      </Text>
                    </Pressable>
                    {Object.keys(bySubcategory).map((key) => {
                      const selected = subcategory === key;
                      return (
                        <Pressable
                          key={key}
                          onPress={() => setSubcategory(selected ? '' : key)}
                          className={`px-3 py-2 rounded-full border ${
                            selected
                              ? 'bg-brand-600 border-brand-600 dark:bg-brand-500 dark:border-brand-500'
                              : 'bg-white border-ink-200 dark:bg-ink-800 dark:border-ink-700'
                          }`}
                          accessibilityRole="button"
                          accessibilityState={{ selected }}
                        >
                          <Text className={`text-small ${
                            selected ? 'text-white font-semibold' : 'text-ink-600 dark:text-ink-300'
                          }`}>
                            {key}
                          </Text>
                        </Pressable>
                      );
                    })}
                  </View>
                </View>
              )}

              {/* Search + clear */}
              <TextInput
                className="border border-ink-200 dark:border-ink-700 rounded-xl px-3 py-3 text-body bg-white dark:bg-ink-800 text-ink-900 dark:text-ink-50"
                value={search}
                onChangeText={setSearch}
                placeholder="搜尋說明 / 標籤"
                placeholderTextColor="#94a3b8"
                returnKeyType="search"
              />
            </ScrollView>
          </Pressable>
        </Pressable>
      </Modal>
    {/* Phase 9.2: 底部 selection bar (selectionMode 才出現, 浮在 list 上) */}
    {selectionMode && (
      <View
        className="absolute bottom-0 left-0 right-0 bg-white dark:bg-ink-900 border-t border-ink-200 dark:border-ink-700 px-4 py-3 flex-row items-center justify-between shadow-pop"
        testID="txn-selection-bar"
      >
        <Pressable
          onPress={exitSelectionMode}
          className="px-3 py-2 -ml-1"
          testID="txn-selection-bar-cancel"
        >
          <Text className="text-ink-600 dark:text-ink-300 text-h3">✕</Text>
        </Pressable>
        <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold">
          已選 {selectedKeys.size} 筆
        </Text>
        <Pressable
          onPress={() => setBulkSheetOpen(true)}
          disabled={selectedKeys.size === 0}
          className={`px-4 py-2 rounded-xl ${
            selectedKeys.size === 0
              ? 'bg-ink-200 dark:bg-ink-700'
              : 'bg-brand-600 active:bg-brand-700'
          }`}
          testID="txn-selection-bar-edit"
        >
          <Text className={`text-body font-semibold ${
            selectedKeys.size === 0 ? 'text-ink-400 dark:text-ink-500' : 'text-white'
          }`}>
            編輯
          </Text>
        </Pressable>
      </View>
    )}
    {/* 橘色 FAB — 對標 MoneyBook 編輯按鈕 (暫不接 backend)。selection mode 時隱藏避免遮 bar */}
    {!selectionMode && (
      <Pressable
        onPress={() => console.log('FAB tapped')}
        className="absolute bottom-6 right-6 w-14 h-14 rounded-full bg-amber-500 dark:bg-amber-600 items-center justify-center shadow-pop active:bg-amber-600 dark:active:bg-amber-700"
        testID="transactions-fab"
      >
        <Text className="text-white text-h2">✏️</Text>
      </Pressable>
    )}
    {/* Phase 9.2: BulkEditSheet — 跨平台 modal, 收 N 筆 Promise.all 連發 single PATCH */}
    <BulkEditSheet
      visible={bulkSheetOpen}
      targets={Array.from(selectedKeys)
        .map((k): BulkTarget | null => {
          const [bank, kind, idStr] = k.split('|');
          const id = parseInt(idStr, 10);
          if (!bank || !kind || Number.isNaN(id)) return null;
          return { bank, kind: kind as 'twd' | 'billed' | 'pending', id };
        })
        .filter((t): t is BulkTarget => t !== null)}
      onClose={() => setBulkSheetOpen(false)}
      onSuccess={(updated, failed, failedTargets) => {
        setBulkSheetOpen(false);
        if (failed === 0) {
          exitSelectionMode();
          // 暫用 console; 未來可改 toast
          console.log(`✅ 批量編輯: ${updated} 筆成功`);
        } else {
          // 保留選擇模式 + 失敗細節，便於使用者重試或檢視
          const failIds = (failedTargets ?? []).map((f) => `${f.target.bank}|${f.target.kind}|${f.target.id}`);
          setSelectedKeys(new Set(failIds));
          console.warn(`⚠️ 批量編輯: ${updated} 筆成功, ${failed} 筆失敗`, failedTargets);
        }
      }}
    />
    {/* L8.5 — Transaction detail / category edit modal */}
    <TxnDetailModal
      txn={detailTxn}
      fxMode={fxMode}
      cardDateBasis={cardDateBasis}
      onClose={() => setDetailTxn(null)}
    />
    </>
  );
}
