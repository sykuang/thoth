/**
 * 儀表板 (L5-RWD upgrade)。
 *
 * 桌機 (lg+): account 卡片 3 col grid
 * 平板 (md): 2 col grid
 * 手機 (xs): 1 col stack
 * 全頁 NativeWind + dark mode token
 */
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useRouter } from 'expo-router';
import { useEffect, useMemo, useRef } from 'react';
import {
  ActivityIndicator,
  Pressable,
  Text,
  View,
} from 'react-native';
import { KeyboardAwareScrollView } from '@/components/KeyboardAwareScrollView';
import { PaymentRemindersCard } from '@/components/PaymentRemindersCard';
import { useBreakpoint } from '@/hooks/useBreakpoint';
import { api } from '@/lib/api';
import { useAuthStore } from '@/stores/auth';
import {
  type BankAccount,
  type Me,
  type PaymentReminder,
  type PortfolioSummary,
  type SyncJob,
} from '@/types/api';

// W (2026-06-17): 砍 JOB_STATUS_LABELS + statusBadgeClass — sync job UI 已改 NeutralBar
// (沒 status badge, 進度只用一條 brand color bar 表示), 兩者皆 unused.

export default function Dashboard() {
  const router = useRouter();
  const bp = useBreakpoint();
  const logout = useAuthStore((s) => s.logout);
  const qc = useQueryClient();

  const meQ = useQuery<Me>({
    queryKey: ['me'],
    queryFn: () => api<Me>('/auth/me'),
  });

  const accountsQ = useQuery<BankAccount[]>({
    queryKey: ['accounts'],
    queryFn: () => api<BankAccount[]>('/accounts'),
  });

  const jobsQ = useQuery<SyncJob[]>({
    queryKey: ['sync', 'jobs'],
    queryFn: () => api<SyncJob[]>('/sync/jobs'),
    refetchInterval: (q) => {
      const data = q.state.data;
      if (!data) return false;
      return data.some((j) => j.status === 'queued' || j.status === 'running') ? 2000 : false;
    },
  });

  // W (2026-06-17): 砍 triggerSync (per-account) + triggerSyncAll —
  // PortfolioHeader 重寫後完全沒同步按鈕了, 同步交給 /sync 頁面處理.

  // Phase 6 Plan A — portfolio summary (淨資產主數字 + 資產 / 負債 / 本月消費)
  // Phase X (2026-06-18): 拿掉 staleTime: 30_000 走 queryClient default (5min)
  // — sync / toggle excluded / bulk edit 都會 invalidate, 5min cache 安全且省 traffic.
  const portfolioQ = useQuery<PortfolioSummary>({
    queryKey: ['portfolio', 'summary'],
    queryFn: () => api<PortfolioSummary>('/portfolio/summary'),
  });

  // L8.5 — KPI: total income / expense / net + 本月支出 / 餐飲 / 卡費未出帳
  type StatsResp = {
    total: number;
    total_income: number;
    total_expense: number;
    total_net: number;
    amount_by_month: Record<string, { income: number; expense: number; net: number; count: number }>;
    amount_by_category: Record<string, number>;
    by_kind: Record<string, number>;
    // Phase 6 (category taxonomy 2026-06-15)
    amount_by_flow_type?: Record<string, number>;
    subscription_total?: number;
    subscription_by_month?: Record<string, number>;
    // Phase 7 (Income 5 類 2026-06-15)
    amount_by_income_category?: Record<string, number>;
    passive_income_total?: number;
    passive_income_by_month?: Record<string, number>;
    passive_income_pct?: number;
    income_unclassified_count?: number;
  };
  const statsQ = useQuery<StatsResp>({
    queryKey: ['transactions', 'stats'],
    queryFn: () => api<StatsResp>('/transactions/stats'),
    // Phase X (2026-06-18): default 5min — sync/categories CRUD invalidate.
  });

  // Phase L10 (2026-06-20): 繳費提醒 (信用卡 auto-debit + 餘額不足/未設定)
  const remindersQ = useQuery<PaymentReminder[]>({
    queryKey: ['auto-debit', 'reminders'],
    queryFn: () => api<PaymentReminder[]>('/cards/auto-debit/reminders'),
    // days_until_due 是日期衍生值；app 跨日後回前景時不能沿用昨天的 cache。
    refetchOnWindowFocus: 'always',
  });

  // W (2026-06-17): 砍 lastJobByAccount — UI 已沒 per-account 最後同步時間顯示
  // (PortfolioHeader 重寫後 metadata 大砍, account 卡片只顯示主號 + 餘額).

  function handleLogout() {
    // L9 (2026-06-21): 主動撤銷 refresh token，避免 token 留在 server DB 直到 30 天過期
    // (fire-and-forget — 即使 server 連不上也照常 local logout)
    const refresh = useAuthStore.getState().refreshToken;
    if (refresh) {
      api('/auth/logout', {
        method: 'POST',
        body: { refresh_token: refresh },
        skipAuth: true,  // logout 不該被自己的 401-retry 拖下水
      }).catch(() => {
        // 撤銷失敗就算了，本地 logout 仍要走
      });
    }
    // L13 (2026-06-22): 主動登出 = 連 Keychain 帳密一起清,
    // 否則下次開 app 進 /login 又被自動 silent re-login 回去 (使用者本意是離開).
    void import('@/lib/credentials').then((m) => m.clearCredentials()).catch(() => undefined);
    logout();
    qc.clear();
    router.replace('/login');
  }

  const ready = (accountsQ.data ?? []).filter((a) => a.has_creds);

  // 是否有任何 job 正在跑 (queued / running) — 給「全部同步」按鈕看
  // (避免重複觸發整批; 個別卡仍可按 — 進 queue 等 backend 序列化執行)
  const hasRunningJob = useMemo(() => {
    return (jobsQ.data ?? []).some(
      (j) => j.status === 'queued' || j.status === 'running',
    );
  }, [jobsQ.data]);

  // Phase X (2026-06-18): sync running → done 邊緣偵測 → invalidate dashboard
  // 用的下游資料 (portfolio.summary / transactions.stats / portfolio.accounts).
  // 之前只在 cards 頁有 transition hook, dashboard 開著時 sync 跑完不會自動刷新,
  // 拉長 staleTime 5min 之後問題更明顯 — 在這裡也補一個.
  const prevHasRunningRef = useRef(false);
  useEffect(() => {
    if (prevHasRunningRef.current && !hasRunningJob) {
      qc.invalidateQueries({ queryKey: ['portfolio'] });
      qc.invalidateQueries({ queryKey: ['transactions'] });
      qc.invalidateQueries({ queryKey: ['frontend-dataset'] });
      qc.invalidateQueries({ queryKey: ['accounts'] });
      qc.invalidateQueries({ queryKey: ['cards'] });
      qc.invalidateQueries({ queryKey: ['auto-debit', 'reminders'] });
    }
    prevHasRunningRef.current = hasRunningJob;
  }, [hasRunningJob, qc]);

  // W (2026-06-17): 砍 allSyncBusy + cardWidth — 已 inline 計算或 grid 改 flex/wrap
  // (PortfolioHeader 重寫後 grid 邏輯也跟著拆掉, 不需要 cardWidth helper).

  return (
    <View className="flex-1 bg-ink-50 dark:bg-ink-950">
    <KeyboardAwareScrollView className="flex-1">
      <View className="px-6 py-6 max-w-[1280px] w-full mx-auto">
        {/* Header */}
        <View className="flex-row items-start mb-6" testID="dashboard-header">
          <View className="flex-1">
            <Text className="text-ink-900 dark:text-ink-50 text-h1">儀表板</Text>
            {meQ.data ? (
              <Text className="text-ink-500 dark:text-ink-400 text-small mt-1">
                目前登入: <Text className="text-ink-900 dark:text-ink-50 font-semibold">{meQ.data.email}</Text>
              </Text>
            ) : meQ.isLoading ? (
              <Text className="text-ink-500 dark:text-ink-400 text-small mt-1">載入中…</Text>
            ) : null}
          </View>
          <Pressable
            onPress={handleLogout}
            className="px-4 py-2 rounded-xl border border-ink-300 dark:border-ink-700 bg-white dark:bg-ink-800"
          >
            <Text className="text-ink-600 dark:text-ink-300 text-small font-semibold">登出</Text>
          </Pressable>
        </View>

        {/* Phase 6 Plan A — Portfolio header (淨資產主數字 + 資產/負債/本月消費) */}
        <PortfolioHeader portfolio={portfolioQ.data} isLoading={portfolioQ.isLoading} bp={bp} />

        {/* L8.5 — KPI 卡 (本月收支詳細 — 補 portfolio 沒有的 income / expense 拆分) */}
        <KpiBar stats={statsQ.data} isLoading={statsQ.isLoading} bp={bp} />

        {/* Phase L10 (2026-06-20) — 繳費提醒 (auto-debit 沒設 / 餘額不足 + 3 天內到期)
            H2 位置: KPI 後, Subscription 前. 空 list 自動 hide. */}
        <PaymentRemindersCard reminders={remindersQ.data ?? []} />

        {/* Phase 6 (category taxonomy 2026-06-15) — 訂閱合計卡 (0 自動隱藏) */}
        <SubscriptionCard stats={statsQ.data} bp={bp} />

        {/* Phase 7 (Income 5 類 FIRE 2026-06-15) — 被動收入卡 (0 自動隱藏) */}
        <PassiveIncomeCard stats={statsQ.data} bp={bp} />

        {/* Phase 8.6 — sync UI 已搬到「帳戶」tab。
            (a) 帳戶 tab header 有「☁️ 全部同步」按鈕
            (b) 每家銀行 group 有 ☁️ 圖示同步該銀行
            (c) 「初次同步」按鈕在 PendingBankAccountRow 就地觸發
            Dashboard 留淨資產 / KPI / 訂閱 / 被動收入 等財務數字面板. */}
        {hasRunningJob && (
          <View className="bg-accent-500/10 dark:bg-accent-500/20 border border-accent-500/30 rounded-xl px-4 py-3 mb-4">
            <View className="flex-row items-center gap-2">
              <ActivityIndicator color="#5B8DEF" size="small" />
              <Text className="text-accent-700 dark:text-accent-400 text-small flex-1">
                同步進行中… 請至「帳戶」tab 查看進度。
              </Text>
              <Pressable
                onPress={() => router.push('/(tabs)/cards')}
                className="bg-accent-600 active:bg-accent-700 rounded-lg px-3 py-1.5"
                testID="dashboard-sync-banner-cta"
              >
                <Text className="text-white text-micro font-semibold">查看 →</Text>
              </Pressable>
            </View>
          </View>
        )}

        {/* Empty state — 還沒任何已 cred 的 account → 引導去帳戶 tab 新增 */}
        {ready.length === 0 && !accountsQ.isLoading && (
          <View className="bg-white dark:bg-ink-900 rounded-2xl p-7 shadow-card items-center mb-4">
            <Text className="text-ink-700 dark:text-ink-200 text-h3 mb-1.5">還沒有任何設定好的帳號</Text>
            <Text className="text-ink-500 dark:text-ink-400 text-small text-center mb-4 leading-5">
              到「帳戶」tab 點「+ 新增銀行帳號」選擇銀行並填密碼{'\n'}
              設好後按「☁️ 全部同步」抓帳, 這裡會顯示資產數字
            </Text>
            <Pressable
              className="bg-brand-600 active:bg-brand-700 rounded-xl px-5 py-2.5 shadow-brand"
              onPress={() => router.push('/(tabs)/cards/new')}
            >
              <Text className="text-white text-small font-semibold">新增銀行帳號</Text>
            </Pressable>
          </View>
        )}
      </View>
    </KeyboardAwareScrollView>
    </View>
  );
}



// ============================================================
// KpiBar — L8.5 dashboard 上方 KPI 卡
// ============================================================
//
// Phase 6 (2026-06-14 PM, 使用者 "三張卡很醜"): 改方案 D
//   單一整合卡 = 「6 月」標題 + 結餘大字 (綠/紅) + 收入/支出 stacked bar + 兩端標 amount
//   不再 3 張獨立小卡, 改成資訊密度高的視覺對比卡, 跟淨資產卡呼應.
//
// 本月 = sorted desc 的 amount_by_month 第一筆 key（避免硬寫 YYYY-MM
// 害 timezone / 假月份顯示 0）

type StatsForKpi = {
  total: number;
  total_income: number;
  total_expense: number;
  total_net: number;
  amount_by_month: Record<string, { income: number; expense: number; net: number; count: number }>;
  amount_by_category: Record<string, number>;
  // Phase 6 (category taxonomy 2026-06-15)
  amount_by_flow_type?: Record<string, number>;  // expense/income/transfer/investment
  subscription_total?: number;
  subscription_by_month?: Record<string, number>;
  // Phase 7 (Income 5 類 2026-06-15) — FIRE 被動收入指標
  amount_by_income_category?: Record<string, number>;  // salary/bonus/interest_dividend/investment_gain/other
  passive_income_total?: number;  // interest_dividend + investment_gain
  passive_income_by_month?: Record<string, number>;
  passive_income_pct?: number;  // passive / total_income * 100 (1 位小數)
  income_unclassified_count?: number;
};

function fmtTWD(n: number): string {
  // 數字 → "$1,234,567" / 負值 → "-$1,234,567"
  const abs = Math.abs(n).toLocaleString('en-US');
  return n < 0 ? `-$${abs}` : `$${abs}`;
}

function KpiBar({
  stats,
  isLoading,
  bp: _bp,
}: {
  stats: StatsForKpi | undefined;
  isLoading: boolean;
  bp: { isLg: boolean; isMd: boolean };
}) {
  if (isLoading) {
    return (
      <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-4">
        <ActivityIndicator />
      </View>
    );
  }
  if (!stats || stats.total === 0) {
    return null;
  }

  // 找本月 (sorted desc 的第一個 key)
  const months = Object.keys(stats.amount_by_month).sort().reverse();
  const currentMonth = months[0] ?? '—';
  const monthData = stats.amount_by_month[currentMonth] ?? {
    income: 0,
    expense: 0,
    net: 0,
    count: 0,
  };

  // 月份 label: "2026-06" → "6 月" (純美化)
  const monthLabel = currentMonth.includes('-')
    ? `${parseInt(currentMonth.split('-')[1], 10)} 月`
    : currentMonth;

  const net = monthData.net;
  const netColor = net >= 0
    ? 'text-accent-600 dark:text-accent-500'
    : 'text-red-600 dark:text-red-400';
  const netStripColor = net >= 0 ? 'bg-accent-500' : 'bg-red-500';

  // === UI 鐵令 (使用者 2026-06-17) ===
  // 兩行極簡:
  //   行 1: 月份 label | 結餘大字(綠/紅)
  //   行 2: 筆數(小字) | (空)
  // 廢除: 收入 vs 支出 stacked bar、結餘/透支 label、紅綠雙 row 數字、bg-ink 條
  // 詳細收支拆分移到 transactions 篩選 + cards 詳情頁
  // 白底 + 4px brand strip(綠/紅 dynamic by 結餘方向)
  return (
    <View
      className="bg-white dark:bg-ink-900 rounded-2xl shadow-card mb-4 overflow-hidden flex-row"
      testID="dashboard-kpi-bar"
    >
      <View className={`w-1 ${netStripColor}`} />
      <View className="flex-1 px-6 py-5">
        {/* 行 1: 月份 + 結餘大字 */}
        <View className="flex-row items-baseline justify-between">
          <Text className="text-ink-500 dark:text-ink-400 text-small font-semibold tracking-wider uppercase">
            {monthLabel}
          </Text>
          <Text
            className={`text-h2 font-bold font-mono ${netColor}`}
            numberOfLines={1}
            adjustsFontSizeToFit
            minimumFontScale={0.6}
          >
            {fmtTWD(net)}
          </Text>
        </View>
        {/* 行 2: 筆數 */}
        <View className="flex-row items-baseline justify-between mt-1">
          <Text className="text-ink-400 dark:text-ink-500 text-micro">
            {monthData.count} 筆交易
          </Text>
        </View>
      </View>
    </View>
  );
}


// ============================================================
// SubscriptionCard (Phase 6 category taxonomy 2026-06-15)
// ============================================================
//
// 顯示「本月訂閱合計」單卡, 對應 wiki § 5.5 設計鐵則 #4.
// is_subscription=1 的 txn aggregate (Netflix/Spotify/iCloud/ChatGPT/...).
//
// 設計取捨:
//   - 本月為主, 副字 (全期 / 上月) 給趨勢感
//   - 全 0 不顯示 (avoid 空卡占版面)
//   - 紫色 (跟 Netflix 紫對齊) — 跟 expense 紅 / income 綠 分流

function SubscriptionCard({
  stats,
  bp: _bp,
}: {
  stats: StatsForKpi | undefined;
  bp: { isLg: boolean; isMd: boolean };
}) {
  if (!stats?.subscription_total) return null;  // 0 或 undefined 不顯示

  const byMonth = stats.subscription_by_month ?? {};
  const months = Object.keys(byMonth).sort().reverse();
  const currentMonth = months[0] ?? '';
  const lastMonth = months[1] ?? '';
  const thisAmt = currentMonth ? (byMonth[currentMonth] ?? 0) : 0;
  const lastAmt = lastMonth ? (byMonth[lastMonth] ?? 0) : 0;

  // === UI 鐵令 (使用者 2026-06-17) ===
  // 兩行極簡:
  //   行 1: 訂閱服務(label) | 本月金額(大字)
  //   行 2: 上月 / 月份(小字) | (空)
  // 廢除: 累計、卡片內 emoji icon、紫色 border、subtotals
  // 白底 + 4px brand strip(violet 對齊訂閱主題色)
  return (
    <View
      className="bg-white dark:bg-ink-900 rounded-2xl shadow-card mb-4 overflow-hidden flex-row"
      testID="dashboard-subscription-card"
    >
      <View className="w-1 bg-violet-500" />
      <View className="flex-1 px-6 py-5">
        {/* 行 1: 訂閱本月 */}
        <View className="flex-row items-baseline justify-between">
          <Text className="text-ink-500 dark:text-ink-400 text-small font-semibold tracking-wider uppercase">
            訂閱
          </Text>
          <Text
            className="text-h2 font-bold font-mono text-violet-700 dark:text-violet-300"
            numberOfLines={1}
            adjustsFontSizeToFit
            minimumFontScale={0.6}
          >
            {fmtTWD(thisAmt)}
          </Text>
        </View>
        {/* 行 2: 月份 + 上月 */}
        <View className="flex-row items-baseline justify-between mt-1">
          <Text className="text-ink-400 dark:text-ink-500 text-micro">
            {currentMonth || '本月'}
          </Text>
          {lastMonth ? (
            <Text className="text-small font-mono text-ink-500 dark:text-ink-400" numberOfLines={1}>
              上月 {fmtTWD(lastAmt)}
            </Text>
          ) : null}
        </View>
      </View>
    </View>
  );
}


// ============================================================
// PassiveIncomeCard (Phase 7 FIRE 被動收入 2026-06-15)
// ============================================================
//
// === UI 鐵令 (使用者 2026-06-17) ===
// 兩行極簡:
//   行 1: 被動收入(label) | 本月金額(大字 emerald)
//   行 2: 本月 % + YTD %(小字) | (空)
// 廢除: Layer 2 5 類分布 + 6 月趨勢 sparkline + emoji + 累計總收入
// 廢除: 卡片內 emoji icon、emerald border、subtotals
// 廢除: tap-to-expand 互動(細項移到 portfolio/passive-income 詳情頁)
// 白底 + 4px brand strip(emerald 對齊被動收入主題色)
//
// 對應 wiki [[income-classifier-and-fire-passive-income-spec]] § 五 Plan A.
// 被動收入 = interest_dividend + investment_gain (FIRE 公式分子).

function PassiveIncomeCard({
  stats,
  bp: _bp,
}: {
  stats: StatsForKpi | undefined;
  bp: { isLg: boolean; isMd: boolean };
}) {
  const now = new Date();
  const currentYear = now.getFullYear();
  const currentMonthKey = `${currentYear}-${String(now.getMonth() + 1).padStart(2, '0')}`;
  const byMonth = stats?.passive_income_by_month ?? {};
  const amountByMonth = stats?.amount_by_month ?? {};
  const passive = byMonth[currentMonthKey] ?? 0;
  if (!passive) return null;  // 本月 0 或 undefined 不顯示

  const monthIncome = amountByMonth[currentMonthKey]?.income ?? 0;
  const passivePct = monthIncome > 0
    ? Math.round((passive / monthIncome) * 1000) / 10
    : 0;
  const ytdPassive = Object.entries(byMonth)
    .filter(([month]) => month.startsWith(`${currentYear}-`))
    .reduce((sum, [, amount]) => sum + amount, 0);
  const ytdIncome = Object.entries(amountByMonth)
    .filter(([month]) => month.startsWith(`${currentYear}-`))
    .reduce((sum, [, bucket]) => sum + bucket.income, 0);
  const ytdPct = ytdIncome > 0
    ? Math.round((ytdPassive / ytdIncome) * 1000) / 10
    : 0;

  return (
    <View
      className="bg-white dark:bg-ink-900 rounded-2xl shadow-card mb-4 overflow-hidden flex-row"
      testID="dashboard-passive-income-card"
    >
      <View className="w-1 bg-emerald-500" />
      <View className="flex-1 px-6 py-5">
        {/* 行 1: 被動收入本月 */}
        <View className="flex-row items-baseline justify-between">
          <Text className="text-ink-500 dark:text-ink-400 text-small font-semibold tracking-wider uppercase">
            被動收入
          </Text>
          <Text
            className="text-h2 font-bold font-mono text-emerald-700 dark:text-emerald-300"
            numberOfLines={1}
            adjustsFontSizeToFit
            minimumFontScale={0.6}
          >
            {fmtTWD(passive)}
          </Text>
        </View>
        {/* 行 2: 本月占比 + YTD */}
        <View className="flex-row items-baseline justify-between mt-1">
          <Text className="text-ink-400 dark:text-ink-500 text-micro">
            本月 {passivePct.toFixed(1)}%
          </Text>
          <Text className="text-small font-mono text-ink-500 dark:text-ink-400" numberOfLines={1}>
            YTD {ytdPct.toFixed(1)}%
          </Text>
        </View>
      </View>
    </View>
  );
}


// ============================================================
// PortfolioHeader (Phase 6 Plan A) — MoneyBook 風淨資產 header
// ============================================================
//
// 設計 (對標 MoneyBook):
//   - 最大字 = 淨資產 (assets - liabilities), 永遠顯示, 跟 MoneyBook header 對齊
//   - 副字三欄: 資產 / 負債 / 本月消費 (純展示, 不點擊)
//   - as_of 標示資料新鮮度, 太舊掛 ⚠️
//
// 關鍵語意 (使用者鐵則 2026-06-14):
//   - 負債 = 上期帳單未繳, NOT 本月已刷
//   - 本月消費 = pending + billed 本月 consume_date, 資訊性, 不扣 net worth
//   - 帳單跟本月消費是兩件事

function fmtNTD(n: number): string {
  // 數字 → "NT$ 1,234,567" / 負值 → "-NT$ 1,234,567"
  const abs = Math.abs(n).toLocaleString('zh-TW');
  return n < 0 ? `-NT$ ${abs}` : `NT$ ${abs}`;
}

function PortfolioHeader({
  portfolio,
  isLoading,
  bp: _bp,
}: {
  portfolio: PortfolioSummary | undefined;
  isLoading: boolean;
  bp: { isLg: boolean; isMd: boolean };
}) {
  if (isLoading) {
    return (
      <View className="bg-white dark:bg-ink-900 rounded-2xl p-6 shadow-card mb-4">
        <ActivityIndicator />
      </View>
    );
  }
  if (!portfolio) return null;

  // 任何資料都沒抓到 → 顯示一張明確空狀態,不要整塊消失讓使用者以為 UI 壞了。
  if (
    portfolio.total_assets === 0 &&
    portfolio.fx_assets_twd === 0 &&
    portfolio.brokerage_assets_twd === 0 &&
    portfolio.total_liabilities === 0 &&
    portfolio.current_month_spending === 0
  ) {
    return (
      <View
        className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-4 border border-amber-100 dark:border-amber-900"
        testID="portfolio-header-empty"
      >
        <Text className="text-ink-900 dark:text-ink-50 text-h3 mb-1">資產統計尚無資料</Text>
        <Text className="text-ink-500 dark:text-ink-400 text-small">
          已登入但目前沒有可統計的銀行餘額或卡片資料。請到「帳戶」確認各銀行是否已同步並有資料列。
        </Text>
      </View>
    );
  }

  // 含外幣估值的淨資產 (使用者 2026-06-14: 「總資產仍只算 TWD 但要加入外幣」)
  // 大字主數字用 net_worth_with_fx, 副字標明含外幣估值
  const netWorthDisplay = portfolio.net_worth_with_fx;
  // W (2026-06-17): 砍 totalAssetsDisplay — 改成兩行極簡後 view 不顯示資產分欄
  const hasFxAssets = portfolio.fx_assets_twd > 0;

  const netWorthColor =
    netWorthDisplay >= 0
      ? 'text-brand-700 dark:text-brand-300'
      : 'text-red-700 dark:text-red-400';

  // 負債明細：信用卡未繳 + 貸款（使用者鐵律：所有爬蟲都要處理貸款）
  const hasLoan = portfolio.total_loan > 0;
  const hasCardUnpaid = portfolio.total_card_unpaid > 0;

  // W (2026-06-17): 砍 balancePanes (asset/liab subRows breakdown) —
  // PortfolioHeader 重寫後改成兩行極簡 (鐵令), 沒空間顯示 subRows.
  // hasFxAssets/hasLoan/hasCardUnpaid 三個 flag 仍保留, view 中其他地方仍用.
  void hasFxAssets; void hasLoan; void hasCardUnpaid;

  // 本月消費屬於「消費流量」domain, 跟資產/負債的「存量」不同維度
  // 抽離成獨立 full-width row, label 左 / value 右 橫排, 不再跟資產/負債硬擠
  const monthSpending = portfolio.current_month_spending;

  // Dashboard header 不在「本月消費」旁顯示 stale warning；該列只顯示更新日期。

  // === UI 鐵令重構（使用者 2026-06-17）===
  // 兩行極簡：
  //   行 1: 淨資產大字
  //   行 2: 本月消費小字
  // 廢除 subRows（資產/負債明細移到 portfolio/cards 頁）
  // 廢除 gradient（白底 + 左 4px brand strip）
  return (
    <View
      className="bg-white dark:bg-ink-900 rounded-2xl shadow-card mb-4 overflow-hidden flex-row"
      testID="portfolio-header"
    >
      {/* 左側 brand strip — UI 鐵令 */}
      <View className="w-1 bg-brand-500" />
      <View className="flex-1 px-6 py-5">
        {/* === 行 1: 淨資產 === */}
        <View className="flex-row items-baseline justify-between">
          <Text className="text-ink-500 dark:text-ink-400 text-small font-semibold tracking-wider uppercase">
            淨資產
          </Text>
          <Text
            className={`text-h2 font-bold font-mono ${netWorthColor}`}
            testID="portfolio-net-worth"
            numberOfLines={1}
            adjustsFontSizeToFit
            minimumFontScale={0.6}
          >
            {fmtNTD(netWorthDisplay)}
          </Text>
        </View>
        {/* === 行 2: 本月消費 + 更新時間（一行內，左 label 右 value）=== */}
        <View className="flex-row items-baseline justify-between mt-1">
          <View className="flex-row items-baseline gap-2">
            <Text className="text-ink-400 dark:text-ink-500 text-micro">本月消費</Text>
            {portfolio.as_of && (
              <Text className="text-ink-300 dark:text-ink-600 text-micro">
                {portfolio.as_of}
              </Text>
            )}
          </View>
          <Text className="text-small font-mono text-accent-600 dark:text-accent-500" numberOfLines={1}>
            {fmtNTD(monthSpending)}
          </Text>
        </View>
      </View>
    </View>
  );
}
