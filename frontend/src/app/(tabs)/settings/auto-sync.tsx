/**
 * 自動同步 (L13 — 2026-06-23 使用者指示).
 *
 * 設計 (覆寫 L12 per-account 版本):
 *   使用者「我不是要每個銀行都有各自的時間 我要使用者設定一個時間給所有帳號」
 *
 * UI:
 *   - 可選 0-3 個固定時段 (10:00 / 12:00 / 18:00 Asia/Taipei)
 *   - 列出當前 user 全部 has_creds account, 強調「都會在這個時間一起同步」
 *   - 顯示上次自動同步時間
 *
 * Endpoints:
 *   GET    /me/sync-preference     → SyncPreference | null
 *   PUT    /me/sync-preference     → upsert {slots, tz?}
 *   DELETE /me/sync-preference     → 204
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Platform,
  Pressable,
  ScrollView,
  Text,
  View,
} from 'react-native';

import { api, ApiError } from '@/lib/api';
import type { BankAccount, SyncPreference, SyncSlot } from '@/types/api';

const BANK_LABEL: Record<string, string> = {
  cathay: '國泰世華', ubot: '聯邦銀行', hsbc: '匯豐銀行',
  ctbc: '中國信託', sinopac: '永豐銀行', scsb: '上海商銀',
  esun: '玉山銀行', taishin: '台新銀行', fubon: '富邦銀行',
  dbs: '星展銀行', scb: '渣打銀行', linebank: 'LINE Bank', rakuten: '樂天國際銀行',
};

const SYNC_SLOTS = [
  { hour: 10, minute: 0, label: '10:00' },
  { hour: 12, minute: 0, label: '12:00' },
  { hour: 18, minute: 0, label: '18:00' },
] as const;

function slotsFromPreference(pref: SyncPreference | null): SyncSlot[] {
  if (!pref) return [];
  if (Array.isArray(pref.slots)) return pref.slots;
  const legacy = `${pad(pref.hour)}:${pad(pref.minute)}` as SyncSlot;
  return pref.enabled && SYNC_SLOTS.some((slot) => slot.label === legacy) ? [legacy] : [];
}

export default function AutoSyncScreen() {
  const qc = useQueryClient();

  const prefQ = useQuery<SyncPreference | null>({
    queryKey: ['sync-preference'],
    queryFn: () => api<SyncPreference | null>('/me/sync-preference'),
  });

  const accountsQ = useQuery<BankAccount[]>({
    queryKey: ['accounts'],
    queryFn: () => api<BankAccount[]>('/accounts'),
  });

  // Local state — 跟 server 同步, 但 user 編輯不立刻 commit (按「儲存」才送).
  // 用 server snapshot 做 derived initial: 第一次有 prefQ.data 時直接設,
  // 之後 user 編輯;若 server 更新 (e.g. saveMut.onSuccess) 也覆寫一次.
  // (lint react-hooks/set-state-in-effect: 用 key-based re-mount 避免 setState in useEffect)
  const dataKey = slotsFromPreference(prefQ.data ?? null).join(',') || 'empty';
  return <AutoSyncBody key={dataKey} pref={prefQ.data ?? null} accountsQ={accountsQ} qc={qc} />;
}

function AutoSyncBody({
  pref,
  accountsQ,
  qc,
}: {
  pref: SyncPreference | null;
  accountsQ: { data?: BankAccount[]; isLoading: boolean };
  qc: ReturnType<typeof useQueryClient>;
}) {
  const initialSlots = slotsFromPreference(pref);
  const [selectedSlots, setSelectedSlots] = useState<SyncSlot[]>(initialSlots);

  const saveMut = useMutation({
    mutationFn: (slots: SyncSlot[]) =>
      api<SyncPreference>('/me/sync-preference', {
        method: 'PUT',
        body: { slots, tz: 'Asia/Taipei' },
      }),
    onSuccess: (data) => {
      qc.setQueryData(['sync-preference'], data);
      Alert.alert(
        '已儲存',
        data.slots.length
          ? `每天 ${data.slots.join('、')} 自動同步全部帳號`
          : '已停用自動同步',
      );
    },
    onError: (e) => {
      const msg = e instanceof ApiError ? String(e.body ?? e.message) : String(e);
      Alert.alert('儲存失敗', msg);
    },
  });


  const isLoading = accountsQ.isLoading;
  if (isLoading) {
    return (
      <View className="flex-1 bg-ink-50 dark:bg-ink-950 items-center justify-center">
        <ActivityIndicator />
      </View>
    );
  }

  const readyAccounts = (accountsQ.data ?? []).filter((a) => a.has_creds);
  const dirty = selectedSlots.join(',') !== initialSlots.join(',');

  function toggleSlot(slot: SyncSlot) {
    setSelectedSlots((current) => (
      current.includes(slot)
        ? current.filter((value) => value !== slot)
        : SYNC_SLOTS.map((value) => value.label).filter(
            (value): value is SyncSlot => current.includes(value) || value === slot,
          )
    ));
  }

  return (
    <ScrollView className="flex-1 bg-ink-50 dark:bg-ink-950">
      <View className="px-6 py-6 max-w-[800px] w-full mx-auto">
        <Text className="text-ink-900 dark:text-ink-50 text-h1 mb-1">自動同步</Text>
        <Text className="text-ink-500 dark:text-ink-400 text-small mb-6">
          每天可選 0–3 個時段同步全部已綁定帳號, 完成後推送通知到手機
        </Text>

        {/* 固定時段 */}
        <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-4">
          <View className="flex-row items-center justify-between mb-3">
            <Text className="text-ink-500 dark:text-ink-400 text-micro font-semibold tracking-wider">
              執行時間 (Asia/Taipei)
            </Text>
            <Text className="text-ink-500 dark:text-ink-400 text-small">
              已選 {selectedSlots.length}/3
            </Text>
          </View>
          <View className="flex-row flex-wrap items-center justify-center gap-3 py-2">
            {SYNC_SLOTS.map((slot) => {
              const selected = selectedSlots.includes(slot.label);
              return (
                <Pressable
                  key={slot.label}
                  accessibilityRole="checkbox"
                  accessibilityState={{ checked: selected }}
                  onPress={() => toggleSlot(slot.label)}
                  testID={`auto-sync-slot-${slot.label}`}
                  className={`rounded-xl border px-5 py-3 ${
                    selected
                      ? 'border-brand-600 bg-brand-50 dark:border-brand-400 dark:bg-brand-950'
                      : 'border-ink-200 bg-white dark:border-ink-700 dark:bg-ink-900'
                  }`}
                >
                  <Text className={`text-h3 ${
                    selected
                      ? 'text-brand-700 dark:text-brand-300'
                      : 'text-ink-700 dark:text-ink-200'
                  }`}>
                    {slot.label}
                  </Text>
                </Pressable>
              );
            })}
          </View>
          <Text className="text-ink-400 dark:text-ink-500 text-caption text-center mt-2">
            可不選；不選即停用自動同步。將同步 {readyAccounts.length} 個已綁定帳號
          </Text>
        </View>

        {/* 對象 account 列表 */}
        <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-4">
          <Text className="text-ink-500 dark:text-ink-400 text-micro font-semibold tracking-wider mb-3">
            將自動同步的帳號 ({readyAccounts.length})
          </Text>
          {readyAccounts.length === 0 ? (
            <Text className="text-ink-500 dark:text-ink-400 text-small italic">
              尚未綁定任何帳號 — 先到「帳戶」分頁新增銀行帳密。
            </Text>
          ) : (
            readyAccounts.map((a) => (
              <View key={a.id} className="flex-row items-center py-2">
                <View className="w-2 h-2 rounded-full bg-accent-500 mr-3" />
                <Text className="text-ink-900 dark:text-ink-50 text-body flex-1">
                  {BANK_LABEL[a.bank] ?? a.bank}
                </Text>
                <Text className="text-ink-500 dark:text-ink-400 text-small">
                  {a.label}
                </Text>
              </View>
            ))
          )}
        </View>

        {/* 上次同步 */}
        {pref?.last_run_at && (
          <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-4">
            <Text className="text-ink-500 dark:text-ink-400 text-micro font-semibold tracking-wider mb-1">
              上次自動同步
            </Text>
            <Text className="text-ink-900 dark:text-ink-50 text-body">
              {formatLocalDateTime(pref.last_run_at)}
            </Text>
          </View>
        )}

        {/* Action buttons */}
        <Pressable
          className={`rounded-xl py-3.5 items-center mt-2 ${
            dirty && !saveMut.isPending
              ? 'bg-brand-600 dark:bg-brand-500 active:bg-brand-700'
              : 'bg-ink-300 dark:bg-ink-700'
          }`}
          disabled={!dirty || saveMut.isPending}
          onPress={() => saveMut.mutate(selectedSlots)}
        >
          {saveMut.isPending ? (
            <ActivityIndicator color="#fff" />
          ) : (
            <Text className="text-white text-h3">儲存設定</Text>
          )}
        </Pressable>


        <Text className="text-ink-400 dark:text-ink-500 text-micro text-center mt-6">
          排程由伺服器背景工作執行, 即使 app 關閉也會執行。
          {'\n'}失敗會推送通知 (不會自動重試)。
        </Text>
      </View>
    </ScrollView>
  );
}

function pad(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

function formatLocalDateTime(iso: string): string {
  try {
    const d = new Date(iso);
    if (Platform.OS === 'web') {
      return d.toLocaleString('zh-TW');
    }
    return d.toLocaleString('zh-TW', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });
  } catch {
    return iso;
  }
}
