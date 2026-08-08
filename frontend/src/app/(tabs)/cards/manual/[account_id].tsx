import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Platform,
  Pressable,
  Switch,
  Text,
  TextInput,
  View,
} from 'react-native';

import { KeyboardAwareScrollView } from '@/components/KeyboardAwareScrollView';
import { api, ApiError, formatApiError } from '@/lib/api';
import { formatDecimal, formatDecimalFixed } from '@/lib/decimal';
import type {
  FinancialAccount,
  FinancialAccountProductType,
  ManualInvestmentHolding,
  ManualInvestmentTransaction,
} from '@/types/api';

type EditableProductType = Exclude<FinancialAccountProductType, 'unknown'>;
type TradeKind = ManualInvestmentTransaction['kind'];

const PRODUCT_TYPES: { value: EditableProductType; label: string }[] = [
  { value: 'deposit', label: '存款' },
  { value: 'time_deposit', label: '定存' },
  { value: 'fx_deposit', label: '外幣存款' },
  { value: 'checking', label: '支票存款' },
  { value: 'loan', label: '貸款' },
  { value: 'mortgage', label: '房貸' },
  { value: 'credit_line', label: '信用額度' },
  { value: 'investment', label: '投資' },
];
const TRADE_KINDS: { value: TradeKind; label: string }[] = [
  { value: 'opening', label: '期初持股' },
  { value: 'buy', label: '買入' },
  { value: 'sell', label: '賣出' },
  { value: 'fee', label: '費用' },
];
const today = () => {
  const date = new Date();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
};

function Field({
  label,
  value,
  onChangeText,
  placeholder,
  keyboardType,
  testID,
}: {
  label: string;
  value: string;
  onChangeText: (value: string) => void;
  placeholder?: string;
  keyboardType?: 'default' | 'decimal-pad';
  testID?: string;
}) {
  return (
    <View className="mb-4">
      <Text className="text-ink-700 dark:text-ink-300 text-small font-medium mb-1.5">{label}</Text>
      <TextInput
        accessibilityLabel={label}
        value={value}
        onChangeText={onChangeText}
        placeholder={placeholder}
        placeholderTextColor="#94a3b8"
        keyboardType={keyboardType}
        autoCapitalize="none"
        className="border border-ink-200 dark:border-ink-700 rounded-xl px-3 py-3 text-body bg-white dark:bg-ink-800 text-ink-900 dark:text-ink-50"
        testID={testID}
      />
    </View>
  );
}

export default function ManualAccountScreen() {
  const { account_id: rawAccountId } = useLocalSearchParams<{ account_id: string }>();
  const accountId = Array.isArray(rawAccountId) ? rawAccountId[0] : rawAccountId;
  const isNew = accountId === 'new';
  const router = useRouter();
  const queryClient = useQueryClient();
  const [productType, setProductType] = useState<EditableProductType>('deposit');
  const [name, setName] = useState('');
  const [currency, setCurrency] = useState('TWD');
  const [balance, setBalance] = useState('0');
  const [included, setIncluded] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const accountsQ = useQuery<FinancialAccount[], ApiError>({
    queryKey: ['financial-accounts', 'manual'],
    queryFn: () => api<FinancialAccount[]>('/financial-accounts?source=manual'),
    enabled: !isNew,
  });
  const account = useMemo(
    () => accountsQ.data?.find((row) => row.id === accountId),
    [accountId, accountsQ.data],
  );

  useEffect(() => {
    if (!account) return;
    // Async query hydrates user-editable state once; derived values would overwrite edits.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (account.product_type !== 'unknown') setProductType(account.product_type);
    setName(account.name);
    setCurrency(account.currency);
    setBalance((account.balance ?? '0').replace(/^-/, ''));
    setIncluded(account.included_in_net_worth);
  }, [account]);

  const saveAccount = useMutation<FinancialAccount, ApiError>({
    mutationFn: () => api<FinancialAccount>(
      isNew ? '/financial-accounts' : `/financial-accounts/${accountId}`,
      {
        method: isNew ? 'POST' : 'PATCH',
        body: {
          product_type: productType,
          name: name.trim(),
          currency: currency.trim().toUpperCase(),
          balance: balance.trim(),
          included_in_net_worth: included,
        },
      },
    ),
    onSuccess: async (saved) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['financial-accounts'] }),
        queryClient.invalidateQueries({ queryKey: ['portfolio', 'summary'] }),
      ]);
      setError(null);
      if (isNew) {
        router.replace({
          pathname: '/(tabs)/cards/manual/[account_id]',
          params: { account_id: saved.id },
        });
      }
    },
    onError: (err) => setError(formatApiError(err)),
  });

  const deleteAccount = useMutation<void, ApiError>({
    mutationFn: () => api(`/financial-accounts/${accountId}`, { method: 'DELETE' }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['financial-accounts'] }),
        queryClient.invalidateQueries({ queryKey: ['portfolio', 'summary'] }),
      ]);
      router.back();
    },
    onError: (err) => setError(formatApiError(err)),
  });

  function confirmDeleteAccount() {
    const message = '此帳戶的持股與交易明細也會刪除，無法復原。';
    if (Platform.OS === 'web') {
      if (window.confirm(message)) deleteAccount.mutate();
      return;
    }
    Alert.alert('刪除手動帳戶？', message, [
      { text: '取消', style: 'cancel' },
      { text: '刪除', style: 'destructive', onPress: () => deleteAccount.mutate() },
    ]);
  }

  if (!isNew && accountsQ.isLoading) {
    return <View className="flex-1 bg-ink-50 dark:bg-ink-950 items-center justify-center"><ActivityIndicator /></View>;
  }
  if (!isNew && accountsQ.isError) {
    return (
      <View className="flex-1 bg-ink-50 dark:bg-ink-950 items-center justify-center p-6">
        <Text className="text-red-600 text-center mb-4">{formatApiError(accountsQ.error)}</Text>
        <Pressable accessibilityRole="button" accessibilityLabel="重試載入手動帳戶" onPress={() => accountsQ.refetch()} className="bg-brand-600 rounded-xl px-4 py-3">
          <Text className="text-white font-semibold">重試</Text>
        </Pressable>
      </View>
    );
  }
  if (!isNew && accountsQ.isSuccess && !account) {
    return <View className="flex-1 bg-ink-50 dark:bg-ink-950 items-center justify-center p-6"><Text className="text-red-600">找不到此手動帳戶</Text></View>;
  }

  return (
    <KeyboardAwareScrollView className="flex-1 bg-ink-50 dark:bg-ink-950">
      <View className="px-4 py-6 max-w-[720px] w-full mx-auto">
        <Text className="text-ink-900 dark:text-ink-50 text-h1 mb-1">
          {isNew ? '新增手動帳戶' : '編輯手動帳戶'}
        </Text>
        <Text className="text-ink-500 dark:text-ink-400 text-small mb-5">
          current valuation 用於淨資產；投資成交價不會冒充目前市價。
        </Text>

        <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-5">
          <Text className="text-ink-900 dark:text-ink-50 text-h2 mb-3">帳戶資料</Text>
          <Text className="text-ink-700 dark:text-ink-300 text-small font-medium mb-2">類型</Text>
          <View className="flex-row flex-wrap gap-2 mb-4">
            {PRODUCT_TYPES.map((option) => (
              <Pressable
                key={option.value}
                onPress={() => setProductType(option.value)}
                accessibilityRole="radio"
                accessibilityState={{ selected: productType === option.value }}
                accessibilityLabel={`帳戶類型：${option.label}`}
                className={`rounded-full px-3 py-2 border ${productType === option.value
                  ? 'bg-brand-600 border-brand-600'
                  : 'bg-white dark:bg-ink-800 border-ink-200 dark:border-ink-700'}`}
              >
                <Text className={productType === option.value ? 'text-white text-small' : 'text-ink-700 dark:text-ink-300 text-small'}>
                  {option.label}
                </Text>
              </Pressable>
            ))}
          </View>
          <Field label="名稱" value={name} onChangeText={setName} placeholder="例如：永豐證券、緊急預備金" testID="manual-name" />
          <View className="flex-row gap-3">
            <View className="w-28"><Field label="幣別" value={currency} onChangeText={setCurrency} placeholder="TWD" /></View>
            <View className="flex-1"><Field label={productType === 'investment' ? '目前總值' : '目前餘額'} value={balance} onChangeText={setBalance} keyboardType="decimal-pad" testID="manual-balance" /></View>
          </View>

          <View className="flex-row items-center justify-between py-2 mb-3">
            <View className="flex-1 pr-3">
              <Text className="text-ink-900 dark:text-ink-50 text-body">納入淨資產</Text>
              <Text className="text-ink-500 dark:text-ink-400 text-micro mt-0.5">關閉後保留帳戶與明細，但不參與總額</Text>
            </View>
            <Switch accessibilityLabel="納入淨資產" value={included} onValueChange={setIncluded} />
          </View>
          {(error || saveAccount.isError || deleteAccount.isError) && (
            <Text className="text-red-600 text-small mb-3">{error ?? formatApiError(saveAccount.error ?? deleteAccount.error)}</Text>
          )}
          <Pressable
            onPress={() => saveAccount.mutate()}
            accessibilityRole="button"
            accessibilityLabel={isNew ? '新增手動帳戶' : '儲存手動帳戶'}
            disabled={saveAccount.isPending || !name.trim() || !balance.trim()}
            className={`bg-brand-600 active:bg-brand-700 rounded-xl py-3 items-center ${saveAccount.isPending ? 'opacity-50' : ''}`}
            testID="save-manual-account"
          >
            {saveAccount.isPending ? <ActivityIndicator color="#fff" /> : <Text className="text-white text-h3">儲存</Text>}
          </Pressable>
        </View>

        {!isNew && account?.product_type === 'investment' && accountId && (
          <InvestmentJournal accountId={accountId} defaultCurrency={account.currency} />
        )}

        {!isNew && (
          <Pressable
            onPress={confirmDeleteAccount}
            accessibilityRole="button"
            accessibilityLabel="刪除手動帳戶"
            disabled={deleteAccount.isPending}
            className="border border-red-300 dark:border-red-800 rounded-xl py-3 items-center mb-8"
            testID="delete-manual-account"
          >
            <Text className="text-red-600 dark:text-red-400 text-h3">刪除手動帳戶</Text>
          </Pressable>
        )}
      </View>
    </KeyboardAwareScrollView>
  );
}

function InvestmentJournal({ accountId, defaultCurrency }: { accountId: string; defaultCurrency: string }) {
  const queryClient = useQueryClient();
  const [editingId, setEditingId] = useState<number | null>(null);
  const [kind, setKind] = useState<TradeKind>('opening');
  const [occurredOn, setOccurredOn] = useState(today());
  const [symbol, setSymbol] = useState('');
  const [quantity, setQuantity] = useState('');
  const [unitPrice, setUnitPrice] = useState('');
  const [amount, setAmount] = useState('');
  const [currency, setCurrency] = useState(defaultCurrency);
  const [note, setNote] = useState('');

  const transactionsQ = useQuery<ManualInvestmentTransaction[], ApiError>({
    queryKey: ['financial-accounts', accountId, 'transactions'],
    queryFn: () => api(`/financial-accounts/${accountId}/transactions`),
  });
  const holdingsQ = useQuery<ManualInvestmentHolding[], ApiError>({
    queryKey: ['financial-accounts', accountId, 'holdings'],
    queryFn: () => api(`/financial-accounts/${accountId}/holdings`),
  });

  function resetTradeForm() {
    setEditingId(null);
    setKind('opening');
    setOccurredOn(today());
    setSymbol('');
    setQuantity('');
    setUnitPrice('');
    setAmount('');
    setCurrency(defaultCurrency);
    setNote('');
  }

  const saveTrade = useMutation<ManualInvestmentTransaction, ApiError>({
    mutationFn: () => api(
      editingId == null
        ? `/financial-accounts/${accountId}/transactions`
        : `/financial-accounts/${accountId}/transactions/${editingId}`,
      {
        method: editingId == null ? 'POST' : 'PATCH',
        body: {
          kind,
          occurred_on: occurredOn,
          symbol: kind === 'fee' ? null : symbol.trim().toUpperCase(),
          quantity: kind === 'fee' ? null : quantity.trim(),
          unit_price: kind === 'buy' || kind === 'sell' ? unitPrice.trim() : null,
          amount: kind === 'fee' ? amount.trim() : null,
          currency: currency.trim().toUpperCase(),
          note: note.trim() || null,
        },
      },
    ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['financial-accounts', accountId, 'transactions'] }),
        queryClient.invalidateQueries({ queryKey: ['financial-accounts', accountId, 'holdings'] }),
      ]);
      resetTradeForm();
    },
  });
  const deleteTrade = useMutation<void, ApiError, number>({
    mutationFn: (id) => api(`/financial-accounts/${accountId}/transactions/${id}`, { method: 'DELETE' }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['financial-accounts', accountId, 'transactions'] }),
        queryClient.invalidateQueries({ queryKey: ['financial-accounts', accountId, 'holdings'] }),
      ]);
      resetTradeForm();
    },
  });

  function editTrade(row: ManualInvestmentTransaction) {
    setEditingId(row.id);
    setKind(row.kind);
    setOccurredOn(row.occurred_on);
    setSymbol(row.symbol ?? '');
    setQuantity(row.quantity ?? '');
    setUnitPrice(row.unit_price ?? '');
    setAmount(row.kind === 'fee' ? row.amount : '');
    setCurrency(row.currency);
    setNote(row.note ?? '');
  }

  function confirmDeleteTrade(id: number) {
    const message = '刪除後會重新計算持股，且無法復原。';
    if (Platform.OS === 'web') {
      if (window.confirm(message)) deleteTrade.mutate(id);
      return;
    }
    Alert.alert('刪除交易？', message, [
      { text: '取消', style: 'cancel' },
      { text: '刪除', style: 'destructive', onPress: () => deleteTrade.mutate(id) },
    ]);
  }

  return (
    <>
      <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-5">
        <Text className="text-ink-900 dark:text-ink-50 text-h2 mb-3">目前持股</Text>
        {holdingsQ.isLoading ? <ActivityIndicator /> : holdingsQ.isError ? (
          <Pressable accessibilityRole="button" accessibilityLabel="重試載入持股" onPress={() => holdingsQ.refetch()}>
            <Text className="text-red-600 text-small">載入失敗，點此重試</Text>
          </Pressable>
        ) : (holdingsQ.data ?? []).length === 0 ? (
          <Text className="text-ink-500 dark:text-ink-400 text-small">尚無持股，先新增期初持股或買入交易。</Text>
        ) : (holdingsQ.data ?? []).map((holding) => (
          <View key={`${holding.symbol}:${holding.currency}`} className="flex-row justify-between py-2 border-b border-ink-100 dark:border-ink-800">
            <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold">{holding.symbol}</Text>
            <Text className="text-ink-700 dark:text-ink-300 text-small">
              {formatDecimal(holding.quantity) ?? holding.quantity} 股 · {holding.currency}
            </Text>
          </View>
        ))}
      </View>

      <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-5">
        <Text className="text-ink-900 dark:text-ink-50 text-h2 mb-3">
          {editingId == null ? '新增交易' : '編輯交易'}
        </Text>
        <View className="flex-row flex-wrap gap-2 mb-4">
          {TRADE_KINDS.map((option) => (
            <Pressable
              key={option.value}
              onPress={() => setKind(option.value)}
              accessibilityRole="radio"
              accessibilityState={{ selected: kind === option.value }}
              accessibilityLabel={`交易類型：${option.label}`}
              className={`rounded-full px-3 py-2 border ${kind === option.value
                ? 'bg-brand-600 border-brand-600'
                : 'bg-white dark:bg-ink-800 border-ink-200 dark:border-ink-700'}`}
            >
              <Text className={kind === option.value ? 'text-white text-small' : 'text-ink-700 dark:text-ink-300 text-small'}>{option.label}</Text>
            </Pressable>
          ))}
        </View>
        <Field label="日期（YYYY-MM-DD）" value={occurredOn} onChangeText={setOccurredOn} />
        {kind !== 'fee' && <Field label="代號" value={symbol} onChangeText={setSymbol} placeholder="例如：AAPL" />}
        {kind !== 'fee' && <Field label="數量" value={quantity} onChangeText={setQuantity} keyboardType="decimal-pad" />}
        {(kind === 'buy' || kind === 'sell') && <Field label="成交單價" value={unitPrice} onChangeText={setUnitPrice} keyboardType="decimal-pad" />}
        {kind === 'fee' && <Field label="費用金額" value={amount} onChangeText={setAmount} keyboardType="decimal-pad" />}
        <Field label="幣別" value={currency} onChangeText={setCurrency} placeholder="USD" />
        <Field label="備註（選填）" value={note} onChangeText={setNote} />
        {saveTrade.isError && <Text className="text-red-600 text-small mb-3">{formatApiError(saveTrade.error)}</Text>}
        <View className="flex-row gap-3">
          {editingId != null && (
            <Pressable accessibilityRole="button" accessibilityLabel="取消編輯交易" onPress={resetTradeForm} className="flex-1 border border-ink-300 dark:border-ink-700 rounded-xl py-3 items-center">
              <Text className="text-ink-700 dark:text-ink-300 text-h3">取消</Text>
            </Pressable>
          )}
          <Pressable
            onPress={() => saveTrade.mutate()}
            accessibilityRole="button"
            accessibilityLabel={editingId == null ? '新增交易' : '儲存交易變更'}
            disabled={saveTrade.isPending}
            className="flex-1 bg-brand-600 active:bg-brand-700 rounded-xl py-3 items-center"
            testID="save-manual-transaction"
          >
            {saveTrade.isPending ? <ActivityIndicator color="#fff" /> : <Text className="text-white text-h3">儲存交易</Text>}
          </Pressable>
        </View>
      </View>

      <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-5">
        <Text className="text-ink-900 dark:text-ink-50 text-h2 mb-3">交易明細</Text>
        {transactionsQ.isLoading ? <ActivityIndicator /> : transactionsQ.isError ? (
          <Pressable accessibilityRole="button" accessibilityLabel="重試載入交易明細" onPress={() => transactionsQ.refetch()}>
            <Text className="text-red-600 text-small">載入失敗，點此重試</Text>
          </Pressable>
        ) : (transactionsQ.data ?? []).length === 0 ? (
          <Text className="text-ink-500 dark:text-ink-400 text-small">尚無交易明細</Text>
        ) : (transactionsQ.data ?? []).map((row) => (
          <View key={row.id} className="py-3 border-b border-ink-100 dark:border-ink-800">
            <View className="flex-row justify-between gap-3">
              <View className="flex-1">
                <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold">
                  {TRADE_KINDS.find((item) => item.value === row.kind)?.label} {row.symbol ?? ''}
                </Text>
                <Text className="text-ink-500 dark:text-ink-400 text-micro mt-0.5">
                  {row.occurred_on}{row.quantity ? ` · ${formatDecimal(row.quantity) ?? row.quantity} 股` : ''}
                  {row.unit_price ? ` · ${row.currency} ${formatDecimalFixed(row.unit_price, 2)}` : ''}
                </Text>
              </View>
              <Text className="text-ink-700 dark:text-ink-300 text-small">
                {row.currency} {formatDecimalFixed(row.amount, 2) ?? row.amount}
              </Text>
            </View>
            <View className="flex-row justify-end gap-3 mt-2">
              <Pressable accessibilityRole="button" accessibilityLabel={`編輯 ${row.symbol ?? ''} 交易`} onPress={() => editTrade(row)}><Text className="text-brand-600 text-small">編輯</Text></Pressable>
              <Pressable accessibilityRole="button" accessibilityLabel={`刪除 ${row.symbol ?? ''} 交易`} onPress={() => confirmDeleteTrade(row.id)} disabled={deleteTrade.isPending}><Text className="text-red-600 text-small">刪除</Text></Pressable>
            </View>
          </View>
        ))}
        {deleteTrade.isError && <Text className="text-red-600 text-small mt-3">{formatApiError(deleteTrade.error)}</Text>}
      </View>
    </>
  );
}
