import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import * as ExpoLinking from 'expo-linking';
import * as WebBrowser from 'expo-web-browser';
import { Platform, Pressable, RefreshControl, ScrollView, Text, View } from 'react-native';

import { api, formatApiError } from '@/lib/api';
import { formatDecimal } from '@/lib/decimal';
import type {
  BrokerageAccount,
  BrokerageActivity,
  BrokerageBalance,
  BrokeragePosition,
  SnapTradePortfolio,
  SnapTradeStatus,
} from '@/types/api';

WebBrowser.maybeCompleteAuthSession();

const EMPTY_PORTFOLIO: SnapTradePortfolio = {
  accounts: [],
  balances: [],
  positions: [],
  activities: [],
  last_synced_at: null,
};
const PORTAL_URL_MAX_AGE_MS = 4 * 60 * 1000;

function money(value: string | null, currency: string | null): string {
  if (value == null) return '—';
  const formatted = formatDecimal(value);
  return formatted == null ? '—' : `${currency ?? ''} ${formatted}`.trim();
}

function accountLabel(account: BrokerageAccount): string {
  return account.number ? `${account.name} · ${account.number}` : account.name;
}

export default function InvestmentsScreen() {
  const queryClient = useQueryClient();
  const statusQuery = useQuery({
    queryKey: ['snaptrade', 'status'],
    queryFn: () => api<SnapTradeStatus>('/snaptrade/status'),
  });
  const portfolioQuery = useQuery({
    queryKey: ['snaptrade', 'portfolio'],
    queryFn: () => api<SnapTradePortfolio>('/snaptrade/portfolio'),
  });
  const sync = useMutation({
    mutationFn: () => api('/snaptrade/sync', { method: 'POST', timeoutMs: 120_000 }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['snaptrade', 'status'] }),
        queryClient.invalidateQueries({ queryKey: ['snaptrade', 'portfolio'] }),
      ]);
    },
  });
  const portal = useMutation({
    mutationFn: ({ redirect_uri, callbackUri }: { redirect_uri: string; callbackUri: string }) =>
      WebBrowser.openAuthSessionAsync(redirect_uri, callbackUri),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ['snaptrade', 'status'] });
      if (result.type === 'success') await sync.mutateAsync();
    },
  });
  const connect = useMutation({
    mutationFn: async () => {
      const callbackUri = ExpoLinking.createURL('/investments', { isTripleSlashed: true });
      const response = await api<{ redirect_uri: string }>('/snaptrade/connect', {
        method: 'POST',
        body: JSON.stringify({ redirect_uri: callbackUri }),
      });
      return { ...response, callbackUri, createdAt: Date.now() };
    },
    onSuccess: async (connection) => {
      // Browsers require popup creation in a direct click handler. Web uses a
      // second explicit click; native can open immediately after URL creation.
      if (Platform.OS !== 'web') portal.mutate(connection);
    },
  });

  const status = statusQuery.data;
  const portfolio = portfolioQuery.data ?? EMPTY_PORTFOLIO;
  const error = connect.error ?? portal.error ?? sync.error ?? statusQuery.error ?? portfolioQuery.error;
  const refreshing = statusQuery.isFetching || portfolioQuery.isFetching || sync.isPending;
  const refresh = () => {
    void Promise.all([statusQuery.refetch(), portfolioQuery.refetch()]);
  };

  return (
    <ScrollView
      className="flex-1 bg-ink-50 dark:bg-ink-950"
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={refresh} />}
    >
      <View className="px-5 py-6 max-w-[900px] w-full mx-auto gap-5">
        <View>
          <Text className="text-ink-900 dark:text-ink-50 text-h1">投資</Text>
          <Text className="text-ink-500 dark:text-ink-400 text-small mt-1">
            透過 SnapTrade 唯讀同步券商帳戶、現金、持倉與交易活動
          </Text>
        </View>

        <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card">
          <Text className="text-ink-900 dark:text-ink-50 text-h3">SnapTrade</Text>
          <Text className="text-ink-500 dark:text-ink-400 text-small mt-1">
            {!status
              ? '讀取連線狀態…'
              : !status.configured
                ? '伺服器尚未設定 SnapTrade'
                : status.connection_count
                  ? `已連結 ${status.connection_count} 個券商連線`
                  : status.registered
                    ? '已建立 SnapTrade 使用者，尚未連結券商'
                    : '尚未開始連結'}
          </Text>
          {status?.last_synced_at && (
            <Text className="text-ink-400 text-micro mt-1">上次同步：{status.last_synced_at}</Text>
          )}
          <View className="flex-row flex-wrap gap-3 mt-4">
            <ActionButton
              label={
                Platform.OS === 'web' && connect.data
                  ? '開啟 SnapTrade'
                  : status?.registered
                    ? '管理券商連線'
                    : '連結券商'
              }
              onPress={() => {
                if (Platform.OS !== 'web' || !connect.data) {
                  connect.mutate();
                  return;
                }
                if (Date.now() - connect.data.createdAt >= PORTAL_URL_MAX_AGE_MS) {
                  connect.mutate();
                  return;
                }
                const connection = connect.data;
                connect.reset();
                portal.mutate(connection);
              }}
              disabled={!status?.configured || connect.isPending || portal.isPending}
            />
            <ActionButton
              label={sync.isPending ? '同步中…' : '同步投資資料'}
              onPress={() => sync.mutate()}
              disabled={!status?.connection_count || sync.isPending}
              secondary
            />
          </View>
          {error && <Text className="text-red-600 text-small mt-3">{formatApiError(error)}</Text>}
        </View>

        {portfolio.accounts.map((account) => (
          <AccountCard
            key={account.id}
            account={account}
            balances={portfolio.balances.filter((row) => row.account_id === account.id)}
            positions={portfolio.positions.filter((row) => row.account_id === account.id)}
          />
        ))}

        {portfolioQuery.isSuccess && portfolio.accounts.length === 0 && status?.configured && (
          <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card">
            <Text className="text-ink-500 dark:text-ink-400 text-body text-center">
              尚無投資資料。先連結券商，再按「同步投資資料」。
            </Text>
          </View>
        )}

        {portfolio.activities.length > 0 && <Activities rows={portfolio.activities} />}
      </View>
    </ScrollView>
  );
}

function ActionButton({
  label,
  onPress,
  disabled,
  secondary = false,
}: {
  label: string;
  onPress: () => void;
  disabled: boolean;
  secondary?: boolean;
}) {
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityState={{ disabled }}
      className={`rounded-xl px-4 py-2.5 active:opacity-70 ${
        disabled
          ? 'bg-ink-200 dark:bg-ink-700'
          : secondary
            ? 'bg-brand-100 dark:bg-brand-900'
            : 'bg-brand-700'
      }`}
    >
      <Text
        className={`font-semibold text-small ${
          disabled
            ? 'text-ink-400'
            : secondary
              ? 'text-brand-800 dark:text-brand-200'
              : 'text-white'
        }`}
      >
        {label}
      </Text>
    </Pressable>
  );
}

function AccountCard({
  account,
  balances,
  positions,
}: {
  account: BrokerageAccount;
  balances: BrokerageBalance[];
  positions: BrokeragePosition[];
}) {
  return (
    <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card">
      <View className="flex-row justify-between gap-3">
        <View className="flex-1">
          <Text className="text-ink-900 dark:text-ink-50 text-h3">{account.institution_name}</Text>
          <Text className="text-ink-500 dark:text-ink-400 text-small mt-0.5">
            {accountLabel(account)}
          </Text>
        </View>
        <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold">
          {money(account.balance_total, account.balance_currency)}
        </Text>
      </View>

      {balances.map((balance) => (
        <View key={balance.currency} className="flex-row justify-between mt-4 pt-3 border-t border-ink-100 dark:border-ink-800">
          <Text className="text-ink-500 dark:text-ink-400 text-small">現金 · {balance.currency}</Text>
          <Text className="text-ink-900 dark:text-ink-50 text-small font-semibold">
            {money(balance.cash, balance.currency)}
          </Text>
        </View>
      ))}

      <View className="mt-4 gap-3">
        {positions.map((position) => (
          <View key={position.provider_symbol_id} className="flex-row items-center justify-between gap-3">
            <View className="flex-1">
              <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold">{position.symbol}</Text>
              <Text className="text-ink-500 dark:text-ink-400 text-micro" numberOfLines={1}>
                數量 {formatDecimal(position.quantity) ?? '—'} · {position.description ?? position.asset_type ?? '持倉'}
              </Text>
            </View>
            <Text className="text-ink-900 dark:text-ink-50 text-small font-semibold">
              {money(position.market_value, position.currency)}
            </Text>
          </View>
        ))}
        {account.holdings_unavailable ? (
          <Text className="text-amber-600 dark:text-amber-400 text-small">
            券商未提供持倉明細；僅顯示券商回報的帳戶總值
          </Text>
        ) : positions.length === 0 && (
          <Text className="text-ink-400 text-small">此帳戶目前沒有證券持倉</Text>
        )}
      </View>

      {!account.activities_supported && (
        <Text className="text-amber-600 dark:text-amber-400 text-micro mt-4">
          此券商未提供可驗證的交易活動 API；不合成交易紀錄。
        </Text>
      )}
    </View>
  );
}

function Activities({ rows }: { rows: BrokerageActivity[] }) {
  return (
    <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-5">
      <Text className="text-ink-900 dark:text-ink-50 text-h3">近期交易活動</Text>
      <Text className="text-ink-400 text-micro mt-1 mb-3">最近一年，顯示最近 50 筆</Text>
      {rows.slice(0, 50).map((row) => (
        <View
          key={`${row.account_id}:${row.id}`}
          className="flex-row justify-between gap-3 py-3 border-b border-ink-100 dark:border-ink-800 last:border-b-0"
        >
          <View className="flex-1">
            <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold">
              {row.type} · {row.symbol ?? row.description ?? '—'}
            </Text>
            <Text className="text-ink-500 dark:text-ink-400 text-micro mt-0.5">
              {row.trade_date?.slice(0, 10) ?? '日期未知'}
              {row.units != null ? ` · ${formatDecimal(row.units) ?? '—'} 單位` : ''}
            </Text>
          </View>
          <Text className="text-ink-900 dark:text-ink-50 text-small font-semibold">
            {money(row.amount, row.currency)}
          </Text>
        </View>
      ))}
    </View>
  );
}
