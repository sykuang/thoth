/**
 * 帳戶 → 管理登入 → /(tabs)/cards/credentials/[bank]
 *
 * 純 single-bank cred 編輯 — 從帳戶 tab 的 ⚙️ 按鈕 push 進來。
 * 原本 settings/index.tsx 是一頁管所有銀行 (chip picker + accountsList +
 * fieldsForm)，這頁簡化為單銀行：直接顯示該銀行底下所有 BankAccount,
 * 點選一個帳號編 cred 欄位。
 *
 * Phase 8.2 (2026-06-15 使用者指示 IA 重整 A 路線): bank cred 管理從設定 tab
 * 搬到帳戶 tab。少了 bank picker, 因為 bank 已由 URL param 鎖定.
 *
 * Backend (沿用):
 *   GET    /accounts                     → [BankAccount, ...] (filter by bank)
 *   POST   /accounts                     body={bank, label}
 *   PUT    /accounts/{id}                body={label}
 *   DELETE /accounts/{id}
 *   PUT    /accounts/{id}/fields         body={field: value}
 *   DELETE /accounts/{id}/fields/{name}
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Stack, useLocalSearchParams, useRouter } from 'expo-router';
import { useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  Text,
  TextInput,
  View,
} from 'react-native';

import { BankBadge } from '@/components/BankBadge';
import { KeyboardAwareScrollView } from '@/components/KeyboardAwareScrollView';
import { useBreakpoint } from '@/hooks/useBreakpoint';
import { api, ApiError, formatApiError } from '@/lib/api';
import {
  type BankAccount,
  type SupportedBank,
  BANK_FIELDS,
  BANK_FIELD_LABELS,
  BANK_LABELS,
  SUPPORTED_BANKS,
} from '@/types/api';

type FormState = Record<string, string>;
const labelOf = (field: string) => BANK_FIELD_LABELS[field] ?? field;

function isSupportedBank(b: string | undefined): b is SupportedBank {
  return typeof b === 'string' && (SUPPORTED_BANKS as readonly string[]).includes(b);
}

export default function PerBankCredentialsScreen() {
  const router = useRouter();
  const qc = useQueryClient();
  const bp = useBreakpoint();
  const params = useLocalSearchParams<{ bank: string }>();
  const bankParam = params.bank;

  // W (2026-06-17): React rules-of-hooks 修正 — 所有 hooks 必須在 conditional
  // return 之前，否則 render order 變化會炸。原版把 isSupportedBank guard
  // 放最上面早 return，但下面的 useState/useQuery/useMutation/useEffect
  // 全部被當成 conditional hooks，eslint react-hooks/rules-of-hooks 抓 17 條。
  //
  // 修法：bank 在 invalid 時用 'cathay' 暫填 (反正下面 view 會被 guard render
  // 蓋掉, 真實 bank 不會被讀到), 讓 hooks 拿到一致的 dep。view 端用變數
  // bankInvalid 做 conditional render 即可。
  const bankInvalid = !isSupportedBank(bankParam);
  const bank: SupportedBank = bankInvalid ? 'cathay' : (bankParam as SupportedBank);
  const bankLabel = BANK_LABELS[bank];

  const [selectedAccountId, setSelectedAccountId] = useState<number | null>(null);
  const [form, setForm] = useState<FormState>({});
  const [newLabel, setNewLabel] = useState('');
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameDraft, setRenameDraft] = useState('');
  const [status, setStatus] = useState<{ kind: 'ok' | 'err'; msg: string } | null>(null);

  const accountsQ = useQuery<BankAccount[]>({
    queryKey: ['accounts'],
    queryFn: () => api<BankAccount[]>('/accounts'),
  });

  const bankAccounts = useMemo(
    () => (accountsQ.data ?? []).filter((a) => a.bank === bank),
    [accountsQ.data, bank],
  );

  // 預設帳號名稱: 「主帳」 或 「帳號 N」
  const suggestedLabel = useMemo(() => {
    const existing = new Set(bankAccounts.map((a) => a.label));
    if (!existing.has('主帳')) return '主帳';
    for (let i = 2; i < 100; i++) {
      const candidate = `帳號 ${i}`;
      if (!existing.has(candidate)) return candidate;
    }
    return '';
  }, [bankAccounts]);

  useEffect(() => {
    // W (2026-06-17): set-state-in-effect — sync newLabel 跟 suggestedLabel.
    // 真實場景: bankAccounts list 變化 → suggestedLabel 重算 → 若 user 沒手動
    // 改過 newLabel (是 '主帳' / '帳號 N' 自動值), 就跟著更新. 算 derived
    // state, 但 newLabel 是 user 可編輯欄, 不能單純 useMemo 取代.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setNewLabel((cur) => {
      if (!cur || cur === '主帳' || cur.startsWith('帳號 ')) return suggestedLabel;
      return cur;
    });
  }, [suggestedLabel]);

  // 自動選第一個帳戶 (若還沒選且有可選)
  useEffect(() => {
    // W (2026-06-17): set-state-in-effect — 初始選擇第一個帳戶.
    // bankAccounts query 完成才知道 list, 必須等到 effect; useMemo
    // 取代會失去「user 切過去再切回來保留選擇」的能力.
    if (selectedAccountId === null && bankAccounts.length > 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setSelectedAccountId(bankAccounts[0].id);
    }
  }, [bankAccounts, selectedAccountId]);

  // Phase C-fe Critical #3 (2026-06-17): 切換 selectedAccountId / bank 時 reset form,
  // 否則 user 在帳號 A 輸入的密碼會被儲存到帳號 B (single user 同銀行多帳號 cred leak)。
  // 例: 永豐 → 主帳 輸入 password=AAA → 切「永豐 → 老婆」 → 補填 user_code=BBB → 儲存
  //      → backend 收到 {password:AAA, user_code:BBB}, AAA 寫進老婆的 cred slot。
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setForm({});
     
    setStatus(null);
     
    setRenamingId(null);
     
    setRenameDraft('');
  }, [selectedAccountId]);

  // bank URL param 變化時也 reset 整個 page state (含 selectedAccountId)
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSelectedAccountId(null);
  }, [bank]);

  const createMut = useMutation<BankAccount, ApiError, { bank: SupportedBank; label: string }>({
    mutationFn: (body) => api<BankAccount>('/accounts', { method: 'POST', body }),
    onSuccess: (newAcct) => {
      qc.invalidateQueries({ queryKey: ['accounts'] });
      setNewLabel('');
      setSelectedAccountId(newAcct.id);
      setStatus({ kind: 'ok', msg: `已新增「${newAcct.label}」` });
    },
    onError: (e) => setStatus({ kind: 'err', msg: formatApiError(e) }),
  });

  const deleteAcctMut = useMutation<void, ApiError, number>({
    mutationFn: (id) => api<void>(`/accounts/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['accounts'] });
      setSelectedAccountId(null);
      setStatus({ kind: 'ok', msg: '已刪除帳號' });
    },
    onError: (e) => setStatus({ kind: 'err', msg: formatApiError(e) }),
  });

  const renameMut = useMutation<BankAccount, ApiError, { id: number; label: string }>({
    mutationFn: ({ id, label }) =>
      api<BankAccount>(`/accounts/${id}`, { method: 'PUT', body: { label } }),
    onSuccess: (acct) => {
      qc.invalidateQueries({ queryKey: ['accounts'] });
      setRenamingId(null);
      setRenameDraft('');
      setStatus({ kind: 'ok', msg: `已改名為「${acct.label}」` });
    },
    onError: (e) => setStatus({ kind: 'err', msg: formatApiError(e) }),
  });

  const putFieldsMut = useMutation<void, ApiError, { id: number; body: FormState }>({
    mutationFn: ({ id, body }) =>
      api<void>(`/accounts/${id}/fields`, { method: 'PUT', body }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['accounts'] });
      setForm({});
      setStatus({ kind: 'ok', msg: '已儲存。' });
    },
    onError: (e) => setStatus({ kind: 'err', msg: formatApiError(e) }),
  });

  const deleteFieldMut = useMutation<void, ApiError, { id: number; field: string }>({
    mutationFn: ({ id, field }) =>
      api<void>(`/accounts/${id}/fields/${field}`, { method: 'DELETE' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['accounts'] });
      setStatus({ kind: 'ok', msg: '欄位已清除' });
    },
    onError: (e) => setStatus({ kind: 'err', msg: formatApiError(e) }),
  });

  function handleCreate() {
    setStatus(null);
    const label = newLabel.trim();
    if (!label) {
      setStatus({ kind: 'err', msg: '請輸入帳號名稱 (例: 主帳 / 老婆 / 公司)' });
      return;
    }
    createMut.mutate({ bank, label });
  }

  function handleSave() {
    setStatus(null);
    if (selectedAccountId === null) return;
    const fields = BANK_FIELDS[bank];
    const body: FormState = {};
    for (const f of fields) {
      const v = (form[f] ?? '').trim();
      if (v) body[f] = v;
    }
    if (Object.keys(body).length === 0) {
      setStatus({ kind: 'err', msg: '請至少填一個欄位再儲存' });
      return;
    }
    putFieldsMut.mutate({ id: selectedAccountId, body });
  }

  const selectedAccount = bankAccounts.find((a) => a.id === selectedAccountId);
  const fields = BANK_FIELDS[bank];
  const useTwoColumn = bp.isLg;

  // ============================================================
  // Subcomponents
  // ============================================================

  const accountsList = (
    <View className="gap-2">
      {bankAccounts.length === 0 ? (
        <View className="bg-ink-50 dark:bg-ink-800/50 rounded-xl p-4 border border-dashed border-ink-200 dark:border-ink-700">
          <Text className="text-ink-500 dark:text-ink-400 text-small text-center">
            {bankLabel} 還沒有任何帳號, 下方輸入名稱按「+ 新增」開始
          </Text>
        </View>
      ) : (
        bankAccounts.map((a) => {
          const isSel = a.id === selectedAccountId;
          return (
            <View
              key={a.id}
              className={`flex-row items-center gap-2 p-3.5 rounded-xl border ${
                isSel
                  ? 'bg-brand-50 dark:bg-brand-950 border-brand-300 dark:border-brand-700'
                  : 'bg-white dark:bg-ink-800 border-ink-200 dark:border-ink-700'
              }`}
            >
              <Pressable className="flex-1" onPress={() => setSelectedAccountId(a.id)}>
                <View className="flex-row items-center gap-2 mb-1">
                  {renamingId === a.id ? (
                    <TextInput
                      value={renameDraft}
                      onChangeText={setRenameDraft}
                      autoFocus
                      maxLength={64}
                      testID={`rename-input-${a.id}`}
                      className="flex-1 border border-brand-500 rounded-lg px-2 py-1 text-h3 bg-white dark:bg-ink-800 text-ink-900 dark:text-ink-50"
                      onSubmitEditing={() => {
                        const next = renameDraft.trim();
                        if (next && next !== a.label) {
                          renameMut.mutate({ id: a.id, label: next });
                        } else {
                          setRenamingId(null);
                          setRenameDraft('');
                        }
                      }}
                    />
                  ) : (
                    <Text
                      className={`text-h3 ${
                        isSel ? 'text-brand-700 dark:text-brand-300' : 'text-ink-900 dark:text-ink-50'
                      }`}
                    >
                      {a.label}
                    </Text>
                  )}
                  {a.has_creds && renamingId !== a.id && (
                    <View className="bg-accent-500/20 dark:bg-accent-500/30 rounded-full px-2 py-0.5">
                      <Text className="text-accent-600 dark:text-accent-500 text-micro font-semibold">
                        ✓ 已設定
                      </Text>
                    </View>
                  )}
                </View>
                <Text className="text-ink-500 dark:text-ink-400 text-micro">
                  {a.has_creds
                    ? `${a.fields_set.map(labelOf).join(' · ')}`
                    : '尚未設定任何欄位'}
                </Text>
              </Pressable>
              {renamingId === a.id ? (
                <>
                  <Pressable
                    onPress={() => {
                      const next = renameDraft.trim();
                      if (next && next !== a.label) {
                        renameMut.mutate({ id: a.id, label: next });
                      } else {
                        setRenamingId(null);
                        setRenameDraft('');
                      }
                    }}
                    disabled={renameMut.isPending}
                    testID={`rename-save-${a.id}`}
                    className="px-3 py-1.5 rounded-lg bg-brand-600 active:bg-brand-500"
                  >
                    <Text className="text-white text-micro font-semibold">儲存</Text>
                  </Pressable>
                  <Pressable
                    onPress={() => {
                      setRenamingId(null);
                      setRenameDraft('');
                    }}
                    className="px-3 py-1.5 rounded-lg border border-ink-300 dark:border-ink-600 bg-white dark:bg-ink-800"
                  >
                    <Text className="text-ink-600 dark:text-ink-300 text-micro">取消</Text>
                  </Pressable>
                </>
              ) : (
                <>
                  <Pressable
                    onPress={() => {
                      setRenamingId(a.id);
                      setRenameDraft(a.label);
                    }}
                    testID={`rename-btn-${a.id}`}
                    className="px-3 py-1.5 rounded-lg border border-brand-300 dark:border-brand-700 bg-white dark:bg-ink-800"
                  >
                    <Text className="text-brand-600 dark:text-brand-400 text-micro font-semibold">改名</Text>
                  </Pressable>
                  <Pressable
                    onPress={() => deleteAcctMut.mutate(a.id)}
                    disabled={deleteAcctMut.isPending}
                    className="px-3 py-1.5 rounded-lg border border-red-300 dark:border-red-800 bg-white dark:bg-ink-800"
                  >
                    <Text className="text-red-600 dark:text-red-400 text-micro font-semibold">刪除</Text>
                  </Pressable>
                </>
              )}
            </View>
          );
        })
      )}

      {/* 新增 input — 在同一銀行下加新帳號 (老婆 / 公司) */}
      <View className="flex-row gap-2 mt-1">
        <TextInput
          className="flex-1 border border-ink-200 dark:border-ink-700 rounded-xl px-3 py-2.5 text-body bg-white dark:bg-ink-800 text-ink-900 dark:text-ink-50"
          value={newLabel}
          onChangeText={setNewLabel}
          placeholder="新帳號名稱 (主帳 / 老婆 / 公司)"
          placeholderTextColor="#94a3b8"
          editable={!createMut.isPending}
          maxLength={64}
          testID="new-account-label-input"
        />
        <Pressable
          className={`bg-accent-600 active:bg-accent-500 rounded-xl px-4 items-center justify-center min-w-[86px] ${
            createMut.isPending ? 'opacity-50' : ''
          }`}
          onPress={handleCreate}
          disabled={createMut.isPending}
          testID="new-account-add-btn"
        >
          {createMut.isPending ? (
            <ActivityIndicator color="#fff" size="small" />
          ) : (
            <Text className="text-white text-h3">+ 新增</Text>
          )}
        </Pressable>
      </View>
    </View>
  );

  const fieldsForm = selectedAccount ? (
    <>
      <View className="mb-3">
        <Text className="text-ink-900 dark:text-ink-50 text-h2 mb-1">
          「{selectedAccount.label}」的登入欄位
        </Text>
        <Text className="text-ink-500 dark:text-ink-400 text-small">
          {selectedAccount.fields_set.length
            ? `已設: ${selectedAccount.fields_set.map((f) => `${labelOf(f)} ●●●●`).join('  ·  ')}`
            : '(尚未設定)'}
        </Text>
      </View>

      <View className="gap-3">
        {fields.map((f) => (
          <View key={f} className="flex-row items-center gap-2">
            <Text className="w-24 text-ink-600 dark:text-ink-300 text-small font-semibold">
              {labelOf(f)}
            </Text>
            <TextInput
              className="flex-1 border border-ink-200 dark:border-ink-700 rounded-xl px-3 py-2.5 text-body bg-white dark:bg-ink-800 text-ink-900 dark:text-ink-50"
              value={form[f] ?? ''}
              onChangeText={(v) => setForm((s) => ({ ...s, [f]: v }))}
              placeholder={
                selectedAccount.fields_set.includes(f)
                  ? '(已設定 — 輸入新值即覆蓋)'
                  : '(尚未設定 — 輸入即新增)'
              }
              placeholderTextColor="#94a3b8"
              secureTextEntry={f === 'password'}
              autoCapitalize="none"
              editable={!putFieldsMut.isPending}
            />
            {selectedAccount.fields_set.includes(f) && (
              <Pressable
                onPress={() => deleteFieldMut.mutate({ id: selectedAccount.id, field: f })}
                disabled={deleteFieldMut.isPending}
                className="px-2.5 py-1.5 rounded-lg border border-red-300 dark:border-red-800"
              >
                <Text className="text-red-600 dark:text-red-400 text-micro font-semibold">清除</Text>
              </Pressable>
            )}
          </View>
        ))}
      </View>

      {status && (
        <View
          className={`mt-4 rounded-lg px-3 py-2 border ${
            status.kind === 'ok'
              ? 'bg-accent-500/10 dark:bg-accent-500/20 border-accent-500/30'
              : 'bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-900'
          }`}
        >
          <Text
            className={`text-small ${
              status.kind === 'ok'
                ? 'text-accent-600 dark:text-accent-500'
                : 'text-red-700 dark:text-red-300'
            }`}
          >
            {status.msg}
          </Text>
        </View>
      )}

      <View className="flex-row gap-3 mt-5">
        <Pressable
          onPress={handleSave}
          disabled={putFieldsMut.isPending}
          className={`bg-brand-600 active:bg-brand-700 rounded-xl px-6 py-3 items-center shadow-brand min-w-[100px] ${
            putFieldsMut.isPending ? 'opacity-50' : ''
          }`}
        >
          {putFieldsMut.isPending ? (
            <ActivityIndicator color="#fff" size="small" />
          ) : (
            <Text className="text-white text-h3">儲存</Text>
          )}
        </Pressable>
      </View>
    </>
  ) : (
    <View className="items-center justify-center py-12">
      <Text className="text-ink-400 dark:text-ink-600 text-h3 mb-2">↑ 請從上方選一個帳號</Text>
      <Text className="text-ink-500 dark:text-ink-400 text-small">
        或新增一個帳號開始填欄位
      </Text>
    </View>
  );

  // W (2026-06-17): bankInvalid 早 return — 在所有 hooks 都呼叫完之後做.
  if (bankInvalid) {
    return (
      <View className="flex-1 bg-ink-50 dark:bg-ink-950 items-center justify-center px-6">
        <Stack.Screen options={{ title: '管理登入' }} />
        <Text className="text-red-600 dark:text-red-400 text-h3 mb-2">
          銀行代碼無效: {String(bankParam)}
        </Text>
        <Text className="text-ink-500 dark:text-ink-400 text-small mb-4">
          請從帳戶 tab 點選銀行的 ⚙️ 按鈕進來
        </Text>
        <Pressable
          onPress={() => router.back()}
          className="bg-brand-600 active:bg-brand-700 rounded-xl px-4 py-2"
        >
          <Text className="text-white text-h3">回上一頁</Text>
        </Pressable>
      </View>
    );
  }

  return (
    <KeyboardAwareScrollView className="flex-1 bg-ink-50 dark:bg-ink-950">
      <Stack.Screen options={{ title: bankLabel }} />
      <View className="px-6 py-6 max-w-[1280px] w-full mx-auto">
        {/* Bank header — BankBadge + 名 + 提示 */}
        <View className="flex-row items-center gap-3 mb-6">
          <BankBadge bank={bank} size="md" />
          <View className="flex-1">
            <Text className="text-ink-900 dark:text-ink-50 text-h1">{bankLabel}</Text>
            <Text className="text-ink-500 dark:text-ink-400 text-small">
              管理該銀行的登入帳號 — 同一家銀行可建多個 (主帳 / 老婆 / 公司)。
              所有帳密 server 端 Fernet 加密保存; API 不會回傳原文。
            </Text>
          </View>
        </View>

        {accountsQ.isLoading ? (
          <View className="bg-white dark:bg-ink-900 rounded-2xl p-6 shadow-card">
            <ActivityIndicator />
          </View>
        ) : accountsQ.isError ? (
          <View className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-2xl p-5">
            <Text className="text-red-700 dark:text-red-300 text-h3 mb-2">讀取帳號失敗</Text>
            <Text className="text-red-700 dark:text-red-400 text-small">
              {formatApiError(accountsQ.error)}
            </Text>
          </View>
        ) : (
          <View className={useTwoColumn ? 'flex-row gap-4' : ''}>
            <View
              className={`bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-4 ${
                useTwoColumn ? 'w-[24rem] mb-0' : ''
              }`}
            >
              <Text className="text-ink-900 dark:text-ink-50 text-h2 mb-3">
                帳號列表
              </Text>
              {accountsList}
            </View>

            <View
              className={`bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-4 ${
                useTwoColumn ? 'flex-1 mb-0' : ''
              }`}
            >
              {fieldsForm}
            </View>
          </View>
        )}
      </View>
    </KeyboardAwareScrollView>
  );
}
