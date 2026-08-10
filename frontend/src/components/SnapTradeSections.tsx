import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import * as ExpoLinking from 'expo-linking';
import * as WebBrowser from 'expo-web-browser';
import { useRouter } from 'expo-router';
import { Platform, Pressable, Text, View } from 'react-native';

import { useOwnerBoundApi } from '@/hooks/useOwnerBoundApi';
import { formatApiError } from '@/lib/api';
import { formatDecimal, formatDecimalFixed } from '@/lib/decimal';
import type {
  BrokerageAccount,
  SnapTradePortfolio,
  SnapTradeStatus,
} from '@/types/api';

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
  const formatted = currency === 'USD' ? formatDecimalFixed(value, 2) : formatDecimal(value);
  return formatted == null ? '—' : `${currency ?? ''} ${formatted}`.trim();
}

function accountLabel(account: BrokerageAccount): string {
  return account.number ? `${account.name} · ${account.number}` : account.name;
}

export function SnapTradeConnectionSettings() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { ownerKey, ownerEpoch, request: ownerApi } = useOwnerBoundApi();
  const statusQuery = useQuery({
    queryKey: ['snaptrade', 'status', ownerKey, ownerEpoch],
    queryFn: () => ownerApi<SnapTradeStatus>('/snaptrade/status'),
    enabled: Boolean(ownerKey),
  });
  const sync = useMutation({
    mutationFn: () => ownerApi('/snaptrade/sync', { method: 'POST', timeoutMs: 120_000 }),
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
      if (result.type === 'success') {
        await sync.mutateAsync();
        router.replace('/(tabs)/cards');
      }
    },
  });
  const connect = useMutation({
    mutationFn: async () => {
      const callbackUri = ExpoLinking.createURL('/investments', { isTripleSlashed: true });
      const response = await ownerApi<{ redirect_uri: string }>('/snaptrade/connect', {
        method: 'POST',
        body: JSON.stringify({ redirect_uri: callbackUri }),
      });
      return { ...response, callbackUri, createdAt: Date.now() };
    },
    onSuccess: (connection) => {
      if (Platform.OS !== 'web') portal.mutate(connection);
    },
  });
  const status = statusQuery.data;
  const error = connect.error ?? portal.error ?? sync.error ?? statusQuery.error;

  return (
    <View>
      <View className="flex-row items-center justify-between gap-3 py-3">
        <View className="flex-1">
          <Text className="text-ink-900 dark:text-ink-50 text-h3">SnapTrade 券商連結</Text>
          <Text className="text-ink-500 dark:text-ink-400 text-small mt-1">
            {!status
              ? '讀取連線狀態…'
              : !status.configured
                ? '伺服器尚未設定 SnapTrade'
                : status.connection_count
                  ? `已連結 ${status.connection_count} 個券商；帳戶總覽顯示於「帳戶」；交易明細顯示於「交易」`
                  : status.registered
                    ? '已建立 SnapTrade 使用者，尚未連結券商'
                    : '尚未開始連結'}
          </Text>
        </View>
        <ActionButton
          label={
            Platform.OS === 'web' && connect.data
              ? '開啟 SnapTrade'
              : status?.registered
                ? '管理連結'
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
          disabled={!status?.configured || connect.isPending || portal.isPending || sync.isPending}
        />
      </View>
      {error && <Text className="text-red-600 text-small pb-3">{formatApiError(error)}</Text>}
    </View>
  );
}

export function SnapTradeAccountsSection() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { ownerKey, ownerEpoch, request: ownerApi } = useOwnerBoundApi();
  const statusQuery = useQuery({
    queryKey: ['snaptrade', 'status', ownerKey, ownerEpoch],
    queryFn: () => ownerApi<SnapTradeStatus>('/snaptrade/status'),
    enabled: Boolean(ownerKey),
  });
  const portfolioQuery = useQuery({
    queryKey: ['snaptrade', 'portfolio', ownerKey, ownerEpoch],
    queryFn: () => ownerApi<SnapTradePortfolio>('/snaptrade/portfolio'),
    enabled: Boolean(ownerKey),
  });
  const sync = useMutation({
    mutationFn: () => ownerApi('/snaptrade/sync', { method: 'POST', timeoutMs: 120_000 }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['snaptrade', 'status'] }),
        queryClient.invalidateQueries({ queryKey: ['snaptrade', 'portfolio'] }),
      ]);
    },
  });
  const status = statusQuery.data;
  const portfolio = portfolioQuery.data ?? EMPTY_PORTFOLIO;
  const hasConnection = Boolean(status?.connection_count);
  const hasSnapshot = portfolio.accounts.length > 0;

  if (
    statusQuery.isSuccess
    && portfolioQuery.isSuccess
    && !hasConnection
    && !hasSnapshot
  ) return null;

  const error = sync.error ?? portfolioQuery.error ?? (!hasSnapshot ? statusQuery.error : null);
  return (
    <View className="mt-6" testID="snaptrade-accounts-section">
      <View className="flex-row items-center justify-between gap-3 mb-3">
        <View className="flex-1">
          <Text className="text-ink-900 dark:text-ink-50 text-h2">券商帳戶</Text>

        </View>
        <ActionButton
          label={sync.isPending ? '同步中…' : '同步券商'}
          onPress={() => sync.mutate()}
          disabled={sync.isPending || (!hasConnection && !hasSnapshot)}
          secondary
        />
      </View>
      {error && <Text className="text-red-600 text-small mb-3">{formatApiError(error)}</Text>}
      {portfolio.accounts.map((account) => (
        <AccountCard
          key={account.id}
          account={account}
          onPress={() => router.push({
            pathname: '/(tabs)/cards/brokerage/[account_id]',
            params: {
              account_id: account.id,
            },
          })}
        />
      ))}
      {portfolioQuery.isSuccess && hasConnection && portfolio.accounts.length === 0 && (
        <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-4">
          <Text className="text-ink-500 dark:text-ink-400 text-body text-center">
            券商已連結，尚無快照資料。請按「同步券商」。
          </Text>
        </View>
      )}
    </View>
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
  onPress,
}: {
  account: BrokerageAccount;
  onPress: () => void;
}) {
  return (
    <Pressable
      onPress={onPress}
      className="bg-white dark:bg-ink-900 rounded-2xl px-4 py-3 shadow-card mb-3 active:opacity-80"
      testID={`brokerage-account-detail-${account.id}`}
      accessibilityRole="button"
      accessibilityLabel={`查看 ${accountLabel(account)} 持股明細`}
    >
      <View className="flex-row items-baseline justify-between gap-3">
        <View className="flex-1 min-w-0">
          <Text className="text-ink-900 dark:text-ink-50 text-body" numberOfLines={1}>
            {account.institution_name}
          </Text>
          <Text className="text-ink-400 dark:text-ink-500 text-micro mt-1" numberOfLines={1}>
            {accountLabel(account)}
          </Text>
        </View>
        <Text className="text-emerald-600 dark:text-emerald-400 text-h3 font-semibold font-mono" numberOfLines={1}>
          {money(account.balance_total, account.balance_currency)}
        </Text>
      </View>
    </Pressable>
  );
}

export function SnapTradeHoldingsSection({ accountId }: { accountId: string }) {
  const { ownerKey, ownerEpoch, request: ownerApi } = useOwnerBoundApi();
  const portfolioQuery = useQuery({
    queryKey: ['snaptrade', 'portfolio', ownerKey, ownerEpoch],
    queryFn: () => ownerApi<SnapTradePortfolio>('/snaptrade/portfolio'),
    enabled: Boolean(ownerKey),
  });
  if (portfolioQuery.isLoading) {
    return <Text className="text-ink-400 text-small py-4">讀取券商持股明細…</Text>;
  }
  if (portfolioQuery.isError) {
    return <Text className="text-red-600 text-small py-4">{formatApiError(portfolioQuery.error)}</Text>;
  }
  const portfolio = portfolioQuery.data ?? EMPTY_PORTFOLIO;
  const account = portfolio.accounts.find((row) => row.id === accountId);
  if (!account) {
    return <Text className="text-ink-400 text-small py-4">找不到券商帳戶</Text>;
  }
  const balances = portfolio.balances.filter((row) => row.account_id === accountId);
  const positions = portfolio.positions.filter((row) => row.account_id === accountId);

  return (
    <View
      className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-5"
      testID="snaptrade-holdings-section"
    >
      <View className="flex-row justify-between gap-3">
        <View className="flex-1">
          <Text className="text-ink-900 dark:text-ink-50 text-h3">{account.institution_name} 持股明細</Text>
          <Text className="text-ink-500 dark:text-ink-400 text-small mt-0.5">
            {accountLabel(account)}
          </Text>
          <Text className="text-ink-400 text-micro mt-1">
            持股資料更新至：{account.synced_at}
          </Text>
        </View>
        <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold">
          {money(account.balance_total, account.balance_currency)}
        </Text>
      </View>
      {balances.map((balance) => (
        <View
          key={balance.currency}
          className="flex-row justify-between mt-4 pt-3 border-t border-ink-100 dark:border-ink-800"
        >
          <Text className="text-ink-500 dark:text-ink-400 text-small">
            現金 · {balance.currency}
          </Text>
          <Text className="text-ink-900 dark:text-ink-50 text-small font-semibold">
            {money(balance.cash, balance.currency)}
          </Text>
        </View>
      ))}
      <View className="mt-4 gap-3">
        {positions.map((position) => (
          <View
            key={position.provider_symbol_id}
            className="flex-row items-center justify-between gap-3"
          >
            <View className="flex-1">
              <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold">
                {position.symbol}
              </Text>
              <Text className="text-ink-500 dark:text-ink-400 text-micro" numberOfLines={1}>
                數量 {formatDecimal(position.quantity) ?? '—'} ·{' '}
                {position.description ?? position.asset_type ?? '持倉'}
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
        ) : positions.length === 0 ? (
          <Text className="text-ink-400 text-small">此帳戶目前沒有證券持倉</Text>
        ) : null}
      </View>
    </View>
  );
}
