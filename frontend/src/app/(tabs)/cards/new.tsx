/**
 * 帳戶 → 新增銀行帳號 → /(tabs)/cards/new
 *
 * Phase 8.2 (2026-06-15 使用者指示): IA 重整 A 路線 — bank cred 管理搬到帳戶 tab,
 * 新增 flow 從這個獨立頁面進入。
 *
 * Flow:
 *   1. 選銀行 (chip)
 *   2. 自動建議 label (主帳 / 帳號 N)
 *   3. POST /accounts → 建 BankAccount → 拿到 id
 *   4. router.replace 到 credentials/[bank], 該 bank 列表自動顯示新帳號
 *
 * 為何 replace 不 push: 不希望使用者 back 還能回到新增頁。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRouter } from 'expo-router';
import { useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  Text,
  TextInput,
  View,
} from 'react-native';
import { KeyboardAwareScrollView } from '@/components/KeyboardAwareScrollView';

import { BankBadge } from '@/components/BankBadge';
import { api, ApiError, formatApiError } from '@/lib/api';
import {
  type BankAccount,
  type SupportedBank,
  BANK_LABELS,
  SUPPORTED_BANKS,
} from '@/types/api';

export default function NewBankAccountScreen() {
  const router = useRouter();
  const qc = useQueryClient();
  const [selectedBank, setSelectedBank] = useState<SupportedBank>('sinopac');
  const [label, setLabel] = useState('');
  const [status, setStatus] = useState<{ kind: 'err'; msg: string } | null>(null);

  const accountsQ = useQuery<BankAccount[]>({
    queryKey: ['accounts'],
    queryFn: () => api<BankAccount[]>('/accounts'),
  });

  // 計算該銀行已有的 label, 自動建議「主帳」或「帳號 N」
  const bankAccounts = useMemo(
    () => (accountsQ.data ?? []).filter((a) => a.bank === selectedBank),
    [accountsQ.data, selectedBank],
  );

  const suggestedLabel = useMemo(() => {
    const existing = new Set(bankAccounts.map((a) => a.label));
    if (!existing.has('主帳')) return '主帳';
    for (let i = 2; i < 100; i++) {
      const candidate = `帳號 ${i}`;
      if (!existing.has(candidate)) return candidate;
    }
    return '';
  }, [bankAccounts]);

  // 切銀行 / accountsQ 載入 → 自動填 suggestion (使用者沒手動覆寫過才覆寫)
  useEffect(() => {
    // W (2026-06-17): set-state-in-effect — derived state, 但 label 是
    // user 可編輯欄, 不能用 useMemo 取代 (會強制覆蓋使用者輸入).
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLabel((cur) => {
      if (!cur || cur === '主帳' || cur.startsWith('帳號 ')) return suggestedLabel;
      return cur;
    });
  }, [suggestedLabel]);

  const createMut = useMutation<BankAccount, ApiError, { bank: SupportedBank; label: string }>({
    mutationFn: (body) => api<BankAccount>('/accounts', { method: 'POST', body }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['accounts'] });
      // replace 不 push — 不要讓 back 回到新增頁
      router.replace(`/(tabs)/cards/credentials/${selectedBank}`);
    },
    onError: (e) => setStatus({ kind: 'err', msg: formatApiError(e) }),
  });

  function handleSubmit() {
    setStatus(null);
    const labelTrim = label.trim();
    if (!labelTrim) {
      setStatus({ kind: 'err', msg: '請輸入帳號名稱 (例: 主帳 / 老婆 / 公司)' });
      return;
    }
    createMut.mutate({ bank: selectedBank, label: labelTrim });
  }

  return (
    <KeyboardAwareScrollView className="flex-1 bg-ink-50 dark:bg-ink-950">
      <View className="px-6 py-6 max-w-[640px] w-full mx-auto">
        <Text className="text-ink-900 dark:text-ink-50 text-h1 mb-2">新增銀行帳號</Text>
        <Text className="text-ink-500 dark:text-ink-400 text-small mb-6">
          選銀行 + 取個名字, 下一步填登入欄位 (ID / 密碼 / 使用者名稱)。
          所有帳密 server 端 Fernet 加密保存。
        </Text>

        {/* Step 1: 選銀行 */}
        <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-4">
          <Text className="text-ink-900 dark:text-ink-50 text-h2 mb-3">1. 選銀行</Text>
          <View className="flex-row flex-wrap gap-2">
            {SUPPORTED_BANKS.map((b) => {
              const isSel = b === selectedBank;
              const count = (accountsQ.data ?? []).filter((a) => a.bank === b).length;
              return (
                <Pressable
                  key={b}
                  onPress={() => setSelectedBank(b)}
                  className={`flex-row items-center gap-2 px-3 py-2 rounded-full border ${
                    isSel
                      ? 'bg-brand-600 border-brand-600 dark:bg-brand-500 dark:border-brand-500'
                      : 'bg-white border-ink-200 dark:bg-ink-800 dark:border-ink-700'
                  }`}
                  testID={`new-bank-${b}`}
                >
                  <BankBadge bank={b} size="xs" />
                  <Text
                    className={`text-small font-medium ${
                      isSel ? 'text-white' : 'text-ink-600 dark:text-ink-300'
                    }`}
                  >
                    {BANK_LABELS[b]}
                    {count > 0 ? ` (${count})` : ''}
                  </Text>
                </Pressable>
              );
            })}
          </View>
        </View>

        {/* Step 2: 名稱 */}
        <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-4">
          <Text className="text-ink-900 dark:text-ink-50 text-h2 mb-1">2. 取個名字</Text>
          <Text className="text-ink-500 dark:text-ink-400 text-micro mb-3">
            同一家銀行可以建多個 — 用「主帳 / 老婆 / 公司」區分
          </Text>
          <TextInput
            className="border border-ink-200 dark:border-ink-700 rounded-xl px-3 py-2.5 text-body bg-white dark:bg-ink-800 text-ink-900 dark:text-ink-50"
            value={label}
            onChangeText={setLabel}
            placeholder="主帳 / 老婆 / 公司"
            placeholderTextColor="#94a3b8"
            editable={!createMut.isPending}
            maxLength={64}
            testID="new-account-label"
          />
        </View>

        {/* Error banner */}
        {status && (
          <View className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-2xl p-4 mb-4">
            <Text className="text-red-700 dark:text-red-300 text-small">{status.msg}</Text>
          </View>
        )}

        {/* Submit */}
        <View className="flex-row gap-3">
          <Pressable
            onPress={() => router.back()}
            disabled={createMut.isPending}
            className="flex-1 bg-white dark:bg-ink-900 border border-ink-200 dark:border-ink-700 rounded-xl py-3 items-center"
          >
            <Text className="text-ink-700 dark:text-ink-300 text-h3">取消</Text>
          </Pressable>
          <Pressable
            onPress={handleSubmit}
            disabled={createMut.isPending}
            className={`flex-1 bg-brand-600 active:bg-brand-700 rounded-xl py-3 items-center shadow-brand ${
              createMut.isPending ? 'opacity-50' : ''
            }`}
            testID="new-account-submit"
          >
            {createMut.isPending ? (
              <ActivityIndicator color="#fff" size="small" />
            ) : (
              <Text className="text-white text-h3">建立並下一步</Text>
            )}
          </Pressable>
        </View>
      </View>
    </KeyboardAwareScrollView>
  );
}
