import { useQuery } from '@tanstack/react-query';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { ActivityIndicator, Pressable, Text, View } from 'react-native';

import { KeyboardAwareScrollView } from '@/components/KeyboardAwareScrollView';
import { api, ApiError, formatApiError } from '@/lib/api';
import type { FinancialAccount, ManualInvestmentTransaction } from '@/types/api';

import { InvestmentJournal } from './[account_id]';

function firstParam(value: string | string[] | undefined): string {
  return Array.isArray(value) ? (value[0] ?? '') : (value ?? '');
}

export default function ManualInvestmentTransactionScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{ account_id?: string; transaction_id?: string }>();
  const accountId = firstParam(params.account_id);
  const rawTransactionId = firstParam(params.transaction_id);
  const transactionId = rawTransactionId === '' ? null : Number(rawTransactionId);
  const transactionIdIsValid = transactionId == null
    || (Number.isInteger(transactionId) && transactionId > 0);

  const accountsQ = useQuery<FinancialAccount[], ApiError>({
    queryKey: ['financial-accounts', 'manual'],
    queryFn: () => api('/financial-accounts?source=manual'),
    refetchOnMount: 'always',
    enabled: accountId !== '',
  });
  const transactionsQ = useQuery<ManualInvestmentTransaction[], ApiError>({
    queryKey: ['financial-accounts', accountId, 'transactions'],
    queryFn: () => api(`/financial-accounts/${accountId}/transactions`),
    enabled: accountId !== '' && transactionIdIsValid && transactionId != null,
    refetchOnMount: 'always',
  });

  const account = accountsQ.data?.find((item) => (
    item.id === accountId && item.source === 'manual' && item.product_type === 'investment'
  ));
  const initialTransaction = transactionId == null
    ? undefined
    : transactionsQ.data?.find((item) => item.id === transactionId);
  const returnToAccount = () => router.dismissTo({
    pathname: '/(tabs)/cards/manual/[account_id]',
    params: { account_id: accountId },
  });
  const loading = accountId !== '' && transactionIdIsValid && (
    !accountsQ.isFetchedAfterMount
    || (transactionId != null && !transactionsQ.isFetchedAfterMount)
  );
  const error = accountId === '' || !transactionIdIsValid
    ? '交易連結無效'
    : accountsQ.isError && accountsQ.data == null
      ? formatApiError(accountsQ.error)
      : transactionId != null && transactionsQ.isError && transactionsQ.data == null
        ? formatApiError(transactionsQ.error)
        : !loading && !account
          ? '找不到手動投資帳戶'
          : !loading && transactionId != null && !initialTransaction
            ? '找不到交易'
            : null;

  return (
    <KeyboardAwareScrollView className="flex-1 bg-ink-50 dark:bg-ink-950">
      <View className="px-4 py-6 max-w-[720px] w-full mx-auto">
        {loading ? <ActivityIndicator /> : error ? (
          <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card">
            <Text className="text-red-600 text-small mb-4">{error}</Text>
            <Pressable
              onPress={() => router.dismissTo('/(tabs)/cards')}
              accessibilityRole="button"
              accessibilityLabel="返回帳戶"
              className="border border-ink-300 dark:border-ink-700 rounded-xl py-3 items-center"
            >
              <Text className="text-ink-700 dark:text-ink-300 text-h3">返回帳戶</Text>
            </Pressable>
          </View>
        ) : account ? (
          <InvestmentJournal
            accountId={account.id}
            defaultCurrency={account.currency}
            mode="create"
            initialTransaction={initialTransaction}
            onSaved={returnToAccount}
            onCancel={returnToAccount}
          />
        ) : null}
      </View>
    </KeyboardAwareScrollView>
  );
}
