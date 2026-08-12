/**
 * AutoDebitSettingModal — per-card-bank 自動扣繳帳號 picker (Phase L10).
 *
 * UX:
 *   - Modal 開啟時 fetch eligible accounts (跨銀行 TWD 活儲)
 *   - Dropdown 選 account → PUT /cards/auto-debit/settings/{card_bank}
 *   - 已有設定 → 顯示「清除」option (DELETE)
 *
 * 注意 (skill expo-react-native-dev rn-modal-chip-invisible-gotcha):
 *   - 不在 Modal 內用 horizontal ScrollView / KeyboardAwareScrollView
 *   - List 直接用 Dropdown (radio-style) 元件
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { ActivityIndicator, Modal, Pressable, Text, View } from 'react-native';

import { Dropdown, type DropdownOption } from '@/components/Dropdown';
import { useOwnerBoundApi } from '@/hooks/useOwnerBoundApi';
import { formatApiError } from '@/lib/api';
import { formatSignedCurrency } from '@/lib/currency';
import {
  type AutoDebitSetting,
  type EligibleAccount,
  BANK_LABELS,
  type SupportedBank,
} from '@/types/api';

type Props = {
  visible: boolean;
  onClose: () => void;
  cardBank: string;
  bankLabel: string;
};

function maskAccount(no: string): string {
  if (no.length <= 4) return no;
  return `****${no.slice(-4)}`;
}

export function AutoDebitSettingModal({ visible, onClose, cardBank, bankLabel }: Props) {
  const qc = useQueryClient();
  const { ownerKey, ownerEpoch, request: ownerApi } = useOwnerBoundApi();
  const [err, setErr] = useState<string | null>(null);

  // 取 user 所有 TWD 活儲帳戶
  const accountsQ = useQuery<EligibleAccount[]>({
    queryKey: ['auto-debit', 'eligible-accounts', ownerKey, ownerEpoch],
    queryFn: () => ownerApi<EligibleAccount[]>('/cards/auto-debit/eligible-accounts'),
    enabled: visible && Boolean(ownerKey),
  });

  // 取目前設定 (確認 currently selected)
  const settingsQ = useQuery<AutoDebitSetting[]>({
    queryKey: ['auto-debit', 'settings', ownerKey, ownerEpoch],
    queryFn: () => ownerApi<AutoDebitSetting[]>('/cards/auto-debit/settings'),
    enabled: visible && Boolean(ownerKey),
  });

  const currentSetting = settingsQ.data?.find((s) => s.card_bank === cardBank);
  const serverSelectedKey = currentSetting
    ? `${currentSetting.account_bank}|${currentSetting.account_no}`
    : '';
  const [draft, setDraft] = useState<{ key: string; value: string } | null>(null);
  const selectedKey = draft?.key === serverSelectedKey ? draft.value : serverSelectedKey;
  const setSelectedKey = (value: string) => {
    setDraft({ key: serverSelectedKey, value });
  };

  const saveMut = useMutation({
    mutationFn: async (vars: { account_bank: string; account_no: string }) => {
      return ownerApi<AutoDebitSetting>(`/cards/auto-debit/settings/${cardBank}`, {
        method: 'PUT',
        body: vars,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['auto-debit', 'settings'] });
      qc.invalidateQueries({ queryKey: ['auto-debit', 'reminders'] });
      onClose();
    },
    onError: (e) => setErr(formatApiError(e)),
  });

  const clearMut = useMutation({
    mutationFn: async () => {
      return ownerApi<void>(`/cards/auto-debit/settings/${cardBank}`, {
        method: 'DELETE',
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['auto-debit', 'settings'] });
      qc.invalidateQueries({ queryKey: ['auto-debit', 'reminders'] });
      onClose();
    },
    onError: (e) => setErr(formatApiError(e)),
  });

  const options: DropdownOption[] = (accountsQ.data ?? []).map((a) => {
    const bLabel = BANK_LABELS[a.bank as SupportedBank] ?? a.bank;
    const nick = a.nickname_overwrite || a.nickname || a.type || '存款帳戶';
    return {
      label: `${bLabel}・${nick}`,
      value: `${a.bank}|${a.account_no}`,
      hint: `${maskAccount(a.account_no)}・${a.raw_balance == null ? '—' : formatSignedCurrency(a.raw_balance, 'TWD')}`,
    };
  });

  const handleSave = () => {
    setErr(null);
    if (!selectedKey) {
      setErr('請選擇扣繳帳戶');
      return;
    }
    const [accBank, accNo] = selectedKey.split('|');
    if (!accBank || !accNo) {
      setErr('帳戶資料無效');
      return;
    }
    saveMut.mutate({ account_bank: accBank, account_no: accNo });
  };

  const handleClear = () => {
    setErr(null);
    clearMut.mutate();
  };

  const busy = saveMut.isPending || clearMut.isPending;

  return (
    <Modal visible={visible} animationType="slide" transparent onRequestClose={onClose}>
      {/* Backdrop */}
      <Pressable
        onPress={onClose}
        className="flex-1 bg-black/50 justify-end"
      >
        {/* Sheet */}
        <Pressable
          onPress={(e) => e.stopPropagation()}
          className="bg-white dark:bg-ink-900 rounded-t-3xl px-5 pt-5 pb-8 max-w-2xl w-full mx-auto"
        >
          <View className="items-center mb-3">
            <View className="w-10 h-1 bg-ink-300 dark:bg-ink-700 rounded-full" />
          </View>
          <Text className="text-ink-900 dark:text-ink-50 text-h2 mb-1">
            設定自動扣繳
          </Text>
          <Text className="text-ink-600 dark:text-ink-400 text-small mb-4">
            {bankLabel}・所有信用卡共用此扣繳帳戶
          </Text>

          {accountsQ.isLoading ? (
            <View className="py-8 items-center">
              <ActivityIndicator />
            </View>
          ) : options.length === 0 ? (
            <View className="py-6">
              <Text className="text-ink-500 dark:text-ink-400 text-small text-center">
                沒有可用的台幣活儲帳戶
              </Text>
              <Text className="text-ink-400 dark:text-ink-500 text-micro text-center mt-1">
                需先同步銀行帳戶 (帳戶 tab)
              </Text>
            </View>
          ) : (
            <View className="mb-4">
              <Dropdown
                label="扣繳帳戶 (跨銀行任選台幣戶)"
                value={selectedKey}
                onChange={setSelectedKey}
                options={options}
                placeholder="請選擇扣繳帳戶"
                testID="auto-debit-account-picker"
              />
            </View>
          )}

          {err && (
            <View className="bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-900 rounded-xl px-3 py-2 mb-3">
              <Text className="text-red-700 dark:text-red-400 text-small">{err}</Text>
            </View>
          )}

          <View className="flex-row gap-2">
            {currentSetting && (
              <Pressable
                onPress={handleClear}
                disabled={busy}
                className="flex-1 py-3 rounded-xl border border-red-300 dark:border-red-800 bg-white dark:bg-ink-900 active:bg-red-50 dark:active:bg-red-950/30 items-center"
                testID="auto-debit-clear-button"
              >
                <Text className="text-red-600 dark:text-red-400 text-body font-semibold">
                  清除設定
                </Text>
              </Pressable>
            )}
            <Pressable
              onPress={handleSave}
              disabled={busy || options.length === 0}
              className={`flex-1 py-3 rounded-xl items-center ${
                busy || options.length === 0
                  ? 'bg-ink-200 dark:bg-ink-800'
                  : 'bg-brand-500 active:bg-brand-600'
              }`}
              testID="auto-debit-save-button"
            >
              {busy ? (
                <ActivityIndicator color="white" />
              ) : (
                <Text className="text-white text-body font-semibold">儲存</Text>
              )}
            </Pressable>
          </View>
        </Pressable>
      </Pressable>
    </Modal>
  );
}
