/**
 * Transaction detail / category edit modal.
 *
 * Extracted from app/(tabs)/transactions.tsx (W Phase 17, 2026-06-17).
 *
 * Tap a row → open this modal. Shows full detail + category/desc/tags editor.
 *
 * Phase 8.2 (2026-06-15 使用者指示 C 路線): 改成純下拉 (主類 chip + 子類 chip),
 *   不再 inline chip 多選, 用 @/components/Dropdown 統一 affordance.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  Text,
  TextInput,
  View,
} from 'react-native';

import { CategoryPicker } from '@/components/CategoryPicker';
import { Dropdown } from '@/components/Dropdown';
import { TagPicker } from '@/components/TagPicker';
import { ApiError, api, formatApiError } from '@/lib/api';
import { sortCategoryKeys } from '@/lib/category-color';
import {
  formatSignedCurrency,
  formatFxRate,
  fxRateSourceLabel,
  renderAmount,
} from '@/lib/currency';
import { maskCardNo } from '@/lib/mask';
import { SCOPE_LABEL, formatTransactionSource, getDisplayDescription } from '@/lib/txnDisplay';
import {
  type CardDateBasis,
  type SupportedBank,
  type Transaction,
  type TransactionSplit,
  type TransactionsListResponse,
  type TransactionsStatsResponse,
  BANK_LABELS,
} from '@/types/api';
import {
  SplitEditor,
  toApiSplits,
  toDraftSplits,
  validateDrafts,
  type DraftSplit,
} from '@/components/transactions/SplitEditor';

export type TxnDetailModalProps = {
  txn: Transaction | null;
  fxMode: import('@/types/api').FxDisplayMode;
  cardDateBasis?: CardDateBasis;
  onClose: () => void;
};

export function TxnDetailModal({
  txn,
  fxMode,
  cardDateBasis = 'consume',
  onClose,
}: TxnDetailModalProps) {
  const qc = useQueryClient();
  const [editCat, setEditCat] = useState('');
  const [editSub, setEditSub] = useState('');
  const [editDesc, setEditDesc] = useState('');  // Phase 8.2: description_overwrite
  const [editTags, setEditTags] = useState<string[]>([]);  // Phase 9: tags
  const [editIgnored, setEditIgnored] = useState(false);  // Phase 9.3: 忽略不納入統計
  const [editSplits, setEditSplits] = useState<DraftSplit[]>([]);  // Phase 10: 分類拆帳
  const [tagPickerVisible, setTagPickerVisible] = useState(false);  // Phase 9.1
  const [status, setStatus] = useState<{ kind: 'ok' | 'err'; msg: string } | null>(null);

  // Phase 8.2: 主類列表 (完整 /rules/categories — 跟 filter 上方共用 source).
  // 2026-07-06: API 為了 distinct 用 SQL ORDER BY category，會把「飲食」排到最底；
  // dropdown 必須跟 filter chips / category summary 共用 life-first canonical order。
  const categoriesQ = useQuery<{ categories: string[] }, ApiError>({
    queryKey: ['rules', 'categories'],
    queryFn: () => api<{ categories: string[] }>('/rules/categories'),
    staleTime: 60_000,
    enabled: txn !== null,
  });
  const categoryOptions = sortCategoryKeys(categoriesQ.data?.categories ?? []).map((c) => ({
    label: c,
    value: c,
  }));

  // Phase 8.2: 子類列表 — 主類選定才撈
  const subcategoriesQ = useQuery<{ subcategories: string[] }, ApiError>({
    queryKey: ['rules', 'subcategories', editCat],
    queryFn: () =>
      api<{ subcategories: string[] }>(
        `/rules/subcategories?category=${encodeURIComponent(editCat)}`,
      ),
    enabled: txn !== null && !!editCat,
    staleTime: 60_000,
  });

  // Phase 10 (2026-07-29) 分類拆帳:
  // 列表回的是「展開後的子項」, 子項 id 是 "{母id}#{序號}" 且不帶 splits。
  // 使用者點子項要編輯時, 必須改的是母筆 —— 這裡把 txn 正規化成「編輯目標」:
  //   子項 → 用 split_of 撈母筆 (帶完整 splits)
  //   母筆 → 直接用
  const isSplitChild = txn?.split_of != null;
  const editTargetId = isSplitChild ? txn?.split_of : txn?.id;
  const parentQ = useQuery<Transaction, ApiError>({
    queryKey: ['transactions', 'detail', txn?.bank, txn?.kind, editTargetId],
    queryFn: () =>
      api<Transaction>(`/transactions/${txn!.bank}/${txn!.kind}/${editTargetId}`),
    enabled: txn !== null && isSplitChild,
    staleTime: 10_000,
  });
  // 子項在母筆載回前先用自己顯示 (避免閃爍), 但編輯欄位以母筆為準
  const editTarget = isSplitChild ? (parentQ.data ?? null) : txn;

  // txn 變了重設輸入框（每次開新 modal）— 必須 useEffect 因為純 setState side effect
  useEffect(() => {
    // W (2026-06-17): set-state-in-effect — modal init pattern.
    // txn prop 變化 (新 row 開 modal) 時 reset form state.
    // 不能用 useMemo (state 是 user 編輯後可變的), 不能用 key 重 mount
    // (modal animation 會炸). 標準 derived-state-reset pattern.
    // Phase 10: 依 editTarget (子項時是母筆) 初始化, 不是 txn 本身。
    if (editTarget) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setEditCat(editTarget.category ?? '');
       
      setEditSub(editTarget.subcategory ?? '');
       
      setEditDesc(editTarget.description_overwrite ?? '');  // Phase 8.2
       
      setEditTags(editTarget.tags ?? []);  // Phase 9
       
      setEditIgnored(editTarget.auto_excluded ?? false);  // Phase 9.3: 沿用 backend flag
       
      setEditSplits(toDraftSplits(editTarget.splits));  // Phase 10
       
      setTagPickerVisible(false);  // Phase 9.1: 重置 picker
       
      setStatus(null);
    }
  }, [editTarget]);

  const patchMut = useMutation<
    Transaction,
    ApiError,
    {
      category: string;
      subcategory: string;
      description_overwrite: string;
      tags: string[];
      auto_excluded: boolean;
      splits: TransactionSplit[];
      /** 這次送出有沒有動到拆帳 — 有的話跳過 optimistic (見下). */
      splitsChanged: boolean;
    },
    { listSnaps: [readonly unknown[], unknown][]; statsSnaps: [readonly unknown[], unknown][] }
  >({
    mutationFn: ({ category, subcategory, description_overwrite, tags, auto_excluded, splits }) => {
      if (!txn || editTargetId == null) throw new Error('no txn');
      // Phase 10: 一律 PATCH 母筆 (子項 id 帶 '#' 後綴, backend 不認)
      return api<Transaction>(
        `/transactions/${txn.bank}/${txn.kind}/${editTargetId}`,
        {
          method: 'PATCH',
          body: { category, subcategory, description_overwrite, tags, auto_excluded, splits },
        },
      );
    },
    // Phase 10 (2026-06-19, 使用者指示): write-through optimistic update — 改 cat 不再等
    // server roundtrip; UI 0ms 更新, server 仍寫. 痛點: invalidate 走 2 個 GET
    // refetch (list + stats) 整盤閃, modal 關掉前等 ~300ms 才看到卡更新。
    //
    // Optimistic 範圍 (保守):
    //   1. 所有 cached ['transactions', queryString] list — 找該 row patch cat/sub/auto_ex
    //   2. 所有 cached ['transactions', 'stats', queryString] — 對 cat 主類 delta:
    //        by_category[old] -=1, [new] +=1
    //        amount_by_category[old] -= abs(amt), [new] += abs(amt)
    //   ❌ Sub 不 optimistic — server 「只在當前 cat filter 下 aggregate sub」
    //      (transactions.py:673), 跨 cat 改易讓 cache 跟 truth 脫節
    //   ❌ auto_excluded 不 optimistic — 牽動 amount_by_month + total_income/expense
    //      + portfolio.summary, delta 複雜易錯
    //   ❌ amount_by_month / total_* 不動 — 改 cat 不影響金流方向
    //   ❌ Phase 10 (2026-07-29): 動到 splits 時整段跳過 optimistic —— 拆帳會讓
    //      「1 個 row 變 N 個 row」且每份金額/分類/是否計入都不同, 這種 cache
    //      重塑無法用 delta 表達, 硬算必錯。直接讓 onSettled 的 invalidate 拿
    //      server truth (多等 ~300ms, 但拆帳本來就是低頻操作)。
    //
    // 安全網: onSettled 仍 invalidate, server truth 在 ~300ms 後回來覆蓋 optimistic,
    // 任何小算錯都會 self-heal.
    onMutate: async (vars) => {
      if (!txn || vars.splitsChanged) return { listSnaps: [], statsSnaps: [] };

      await qc.cancelQueries({ queryKey: ['transactions'] });

      const oldCat = txn.category ?? '';
      const newCat = vars.category ?? '';
      const newSub = vars.subcategory ?? '';
      const absAmt = Math.abs(txn.amount ?? 0);

      // 1. List cache patch — 找該 row 改 cat/sub/auto_ex
      const listSnaps = qc.getQueriesData<TransactionsListResponse>({
        queryKey: ['transactions'],
      }).filter(([key]) => {
        // key 形狀: ['transactions', queryString] (list) vs ['transactions', 'stats', ...]
        // 只挑 list, 跳過 stats / tags
        return Array.isArray(key) && key.length === 2 && typeof key[1] === 'string';
      });
      for (const [key, data] of listSnaps) {
        if (!data?.items) continue;
        const next = {
          ...data,
          items: data.items.map((t: Transaction) =>
            t.bank === txn.bank && t.kind === txn.kind && t.id === txn.id
              ? {
                  ...t,
                  category: newCat || null,
                  subcategory: newSub || null,
                  description_overwrite: vars.description_overwrite || null,
                  tags: vars.tags,
                  auto_excluded: vars.auto_excluded,
                }
              : t,
          ),
        };
        qc.setQueryData(key, next);
      }

      // 2. Stats cache patch — cat 主類 delta (count + amount)
      const statsSnaps = qc.getQueriesData<TransactionsStatsResponse>({
        queryKey: ['transactions', 'stats'],
      });
      if (oldCat !== newCat) {
        for (const [key, data] of statsSnaps) {
          if (!data) continue;
          const next: TransactionsStatsResponse = {
            ...data,
            by_category: { ...data.by_category },
            ...(data.amount_by_category && {
              amount_by_category: { ...data.amount_by_category },
            }),
          };
          if (oldCat) {
            next.by_category[oldCat] = (next.by_category[oldCat] ?? 1) - 1;
            if (next.by_category[oldCat] <= 0) delete next.by_category[oldCat];
            if (next.amount_by_category) {
              next.amount_by_category[oldCat] =
                (next.amount_by_category[oldCat] ?? 0) - absAmt;
              if (next.amount_by_category[oldCat] <= 0) {
                delete next.amount_by_category[oldCat];
              }
            }
          }
          if (newCat) {
            next.by_category[newCat] = (next.by_category[newCat] ?? 0) + 1;
            if (next.amount_by_category) {
              next.amount_by_category[newCat] =
                (next.amount_by_category[newCat] ?? 0) + absAmt;
            }
          }
          qc.setQueryData(key, next);
        }
      }

      return { listSnaps, statsSnaps };
    },
    onError: (e, _vars, ctx) => {
      // Rollback: snapshot 全部還原
      if (ctx) {
        for (const [key, data] of ctx.listSnaps) qc.setQueryData(key, data);
        for (const [key, data] of ctx.statsSnaps) qc.setQueryData(key, data);
      }
      setStatus({ kind: 'err', msg: formatApiError(e) });
    },
    onSuccess: () => {
      setStatus({ kind: 'ok', msg: '已儲存' });
      // 給 250ms 顯示提示, 再關 modal
      setTimeout(onClose, 350);
    },
    onSettled: () => {
      // Server truth 覆蓋 optimistic — 任何 sub / auto_excluded 沒 optimistic 的欄位
      // 在這裡 self-heal. portfolio.summary 也走這條.
      qc.invalidateQueries({ queryKey: ['transactions'] });
      qc.invalidateQueries({ queryKey: ['frontend-dataset'] });
      qc.invalidateQueries({ queryKey: ['transactions', 'stats'] });
      qc.invalidateQueries({ queryKey: ['portfolio', 'summary'] });
    },
  });

  if (!txn) return null;

  const render = renderAmount(txn, fxMode);
  const postDate = txn.post_date?.trim() || null;
  const [rawDisplayDescription] = getDisplayDescription({
    ...txn,
    description_overwrite: null,
  });
  const amountColor =
    render.direction === 'income'
      ? 'text-accent-600 dark:text-accent-500'
      : render.direction === 'expense'
        ? 'text-red-600 dark:text-red-400'
        : 'text-ink-500 dark:text-ink-400';

  // 判斷是否外幣消費 — 決定要不要顯示 fx_rate row
  const isForeign =
    txn.consume_currency != null &&
    txn.consume_currency !== 'TWD' &&
    txn.consume_amount != null &&
    txn.consume_amount !== 0;

  // 比對原 txn 判斷是否「有改動」(submit button enabled state)
  // Phase 10: 一律比對 editTarget (子項時是母筆), 因為送出的也是母筆。
  // editTarget 為 null = 子項的母筆還在載入 → 先不讓存。
  const base = editTarget;
  const origTags = base?.tags ?? [];
  const tagsDiffer =
    editTags.length !== origTags.length ||
    editTags.some((t, i) => t !== origTags[i]);

  // Phase 10 分類拆帳
  // 母筆金額 (絕對值) — 拆帳總和必須等於它。用 cashflow_amount (使用者視角),
  // 不用 raw amount (信用卡 raw 是帳單視角, 跟畫面顯示的數字不一致)。
  const splitParentAmount = Math.abs(
    base?.cashflow_amount ?? base?.amount ?? 0,
  );
  const origSplits = toDraftSplits(base?.splits);
  const splitsChanged =
    editSplits.length !== origSplits.length ||
    editSplits.some((d, i) => {
      const o = origSplits[i];
      return (
        d.amount !== o.amount ||
        d.category !== o.category ||
        d.note !== o.note ||
        d.auto_excluded !== o.auto_excluded
      );
    });
  const splitError = validateDrafts(editSplits, splitParentAmount);

  const hasChange =
    base != null &&
    (editCat !== (base.category ?? '') ||
      editSub !== (base.subcategory ?? '') ||
      editDesc.trim() !== (base.description_overwrite ?? '') ||
    tagsDiffer ||
      editIgnored !== (base.auto_excluded ?? false) ||
      splitsChanged);

  // body content — 兩個分支 (iOS pageSheet / web 浮動) 共用
  const bodyContent = (
    <>
          {/* Header — Phase 8.2: overwrite 顯示在主標, 原文當副標 (有覆寫才顯示) */}
          <View className="flex-row items-start justify-between mb-4">
            <View className="flex-1">
              <Text className="text-ink-900 dark:text-ink-50 text-h2 font-bold mb-1">
                {(() => {
                  const [shown, edited] = getDisplayDescription(txn);
                  return edited ? `✏️ ${shown}` : shown;
                })()}
              </Text>
              {txn.description_overwrite && rawDisplayDescription !== '—' ? (
                <Text className="text-ink-400 dark:text-ink-500 text-micro mb-1 italic">
                  原文: {rawDisplayDescription}
                </Text>
              ) : null}
              <View className="flex-row items-center gap-2 flex-wrap">
                <Text className="text-ink-500 dark:text-ink-400 text-small">
                  {formatTransactionSource(
                    BANK_LABELS[txn.bank as SupportedBank] ?? txn.bank,
                    {
                      kind: txn.kind,
                      accountNo: txn.account_no,
                      accountOrCard: txn.account_or_card,
                    },
                  )}
                </Text>
                <Text className="text-ink-300 dark:text-ink-600">·</Text>
                <Text className="text-ink-500 dark:text-ink-400 text-small">
                  {txn.date ?? '—'}
                </Text>
              </View>
            </View>
            <Pressable onPress={onClose} className="px-2 py-1 -mr-2 -mt-1">
              <Text className="text-ink-500 dark:text-ink-400 text-h3">✕</Text>
            </Pressable>
          </View>

          {/* Amount card — 大字 primary + 灰色 sub (依 fxMode 變) */}
          <View className="bg-ink-50 dark:bg-ink-800 rounded-xl p-4 mb-4 items-center">
            <Text className={`text-h1 font-mono font-bold ${amountColor}`}>
              {render.primary}
            </Text>
            {render.sub && (
              <Text className="text-ink-500 dark:text-ink-400 text-small font-mono mt-1">
                {render.sub}
              </Text>
            )}
          </View>

          {/* fx_rate 透明度 — 外幣才顯示, 包含「無匯率」訊息 */}
          {isForeign && (
            <View
              className={`rounded-xl px-3 py-2.5 mb-4 border ${
                txn.fx_rate_source === 'bank_billed'
                  ? 'bg-accent-500/10 border-accent-500/30'
                  : txn.fx_rate_source === 'bank_pending_estimate'
                    ? 'bg-amber-500/10 border-amber-500/30 dark:bg-amber-500/15'
                    : 'bg-ink-100 dark:bg-ink-800 border-ink-200 dark:border-ink-700'
              }`}
            >
              <Text className="text-ink-500 dark:text-ink-400 text-micro font-semibold tracking-wider uppercase mb-1">
                匯率資訊
              </Text>
              {txn.fx_rate != null && txn.consume_currency ? (
                <>
                  <Text className="text-ink-900 dark:text-ink-50 text-small font-mono">
                    {formatFxRate(txn.fx_rate, txn.consume_currency)}
                  </Text>
                  {txn.fx_rate_source && (
                    <Text className="text-ink-600 dark:text-ink-400 text-micro mt-0.5">
                      {fxRateSourceLabel(txn.fx_rate_source)}
                    </Text>
                  )}
                </>
              ) : (
                <Text className="text-ink-700 dark:text-ink-300 text-small">
                  ⏳ 本筆未出帳, 出帳後才會有銀行實際匯率 (本系統不推算)
                </Text>
              )}
            </View>
          )}

          {/* 細節 grid */}
          <View className="gap-2 mb-4">
            {txn.account_or_card && (
              <DetailRow label="帳號 / 卡號" value={maskCardNo(txn.account_or_card)} mono />
            )}
            {txn.balance != null && (
              <DetailRow label="帳戶餘額" value={formatSignedCurrency(txn.balance, 'TWD')} mono />
            )}
            {txn.consume_date && <DetailRow label="消費日" value={txn.consume_date} />}
            {(txn.kind === 'billed' || txn.kind === 'pending') && (
              <>
                <DetailRow label="入帳日" value={postDate ?? '尚無入帳日'} />
                <DetailRow
                  label="認列方式"
                  value={cardDateBasis === 'post' && !postDate
                    ? '暫按消費日（尚未取得入帳日）'
                    : cardDateBasis === 'post' ? '入帳日' : '消費日'}
                />
              </>
            )}
            {txn.scope && <DetailRow label="範圍" value={SCOPE_LABEL[txn.scope] ?? txn.scope} />}
            {txn.datetime && <DetailRow label="完整時間" value={txn.datetime} />}
          </View>

          {/* Phase 8.2: 說明覆寫 (overwrite) — raw description 永遠不動 */}
          <View className="mb-4">
            <View className="flex-row items-center justify-between mb-2">
              <Text className="text-ink-700 dark:text-ink-300 text-small font-semibold">
                說明 (可覆寫)
              </Text>
              {editDesc.length > 0 ? (
                <Pressable
                  onPress={() => setEditDesc('')}
                  className="px-2 py-1 rounded-lg bg-ink-100 dark:bg-ink-800 border border-ink-200 dark:border-ink-700"
                  testID="txn-detail-desc-reset"
                >
                  <Text className="text-ink-600 dark:text-ink-400 text-micro">
                    ↺ 重設
                  </Text>
                </Pressable>
              ) : null}
            </View>
            <TextInput
              value={editDesc}
              onChangeText={setEditDesc}
              placeholder={rawDisplayDescription}
              placeholderTextColor="#94a3b8"
              maxLength={200}
              testID="txn-detail-desc-input"
              className="border border-ink-200 dark:border-ink-700 rounded-xl px-3 py-2.5 text-body bg-white dark:bg-ink-800 text-ink-900 dark:text-ink-50"
            />
            <Text className="text-ink-400 dark:text-ink-500 text-micro mt-1">
              留空恢復原文 · 原始資料永遠保留
            </Text>
          </View>

          {/* Category 編輯 — Phase 8.x 改純 dropdown 解決 chip 看起來像多選的 affordance 問題 */}
          <View className="mb-3">
            <CategoryPicker
              label="主分類"
              value={editCat}
              onChange={(next) => {
                setEditCat(next);
                // 換主類自動清子類, 避免「餐廳」套到「交通」
                if (next !== editCat) setEditSub('');
              }}
              options={categoryOptions}
              placeholder={categoriesQ.isLoading ? '載入中…' : '請選擇主分類'}
              disabled={categoriesQ.isLoading}
              testID="txn-detail-category-dropdown"
              modalTitle="選擇分類"
            />

            {/* 子類 — 選了主類才顯示 */}
            {editCat ? (
              <View className="mt-3">
                <Dropdown
                  label="子分類"
                  value={editSub}
                  onChange={setEditSub}
                  options={(subcategoriesQ.data?.subcategories ?? []).map((s) => ({
                    label: s,
                    value: s,
                  }))}
                  placeholder={
                    subcategoriesQ.isLoading
                      ? '載入中…'
                      : (subcategoriesQ.data?.subcategories ?? []).length === 0
                        ? '此主類沒有子分類'
                        : '(無子分類)'
                  }
                  disabled={
                    subcategoriesQ.isLoading ||
                    (subcategoriesQ.data?.subcategories ?? []).length === 0
                  }
                  clearLabel="(無子分類)"
                  testID="txn-detail-subcategory-dropdown"
                  modalTitle="選擇子分類"
                />
              </View>
            ) : (
              <Text className="text-ink-400 dark:text-ink-500 text-micro mt-2">
                (請先選主分類)
              </Text>
            )}
          </View>

          {/* Phase 9.1 (2026-06-17): 標籤 — 點開 TagPicker, 不再 inline 重打 */}
          <View className="mb-4">
            <Text className="text-ink-700 dark:text-ink-300 text-small font-semibold mb-2">
              標籤
            </Text>
            <Pressable
              onPress={() => setTagPickerVisible(true)}
              className="border border-ink-200 dark:border-ink-700 rounded-xl px-3 py-2.5 bg-white dark:bg-ink-800 active:bg-ink-50 dark:active:bg-ink-700"
              testID="txn-detail-tag-picker-open"
            >
              {editTags.length === 0 ? (
                <Text className="text-ink-400 dark:text-ink-500 text-body">
                  ＋ 選擇或新增標籤
                </Text>
              ) : (
                <View className="flex-row items-center justify-between">
                  <View className="flex-row flex-wrap gap-1.5 flex-1 mr-2">
                    {editTags.map((tag) => (
                      <View
                        key={tag}
                        className="bg-brand-100 dark:bg-brand-950 rounded-full px-2.5 py-0.5"
                      >
                        <Text className="text-brand-700 dark:text-brand-300 text-micro font-semibold">
                          #{tag}
                        </Text>
                      </View>
                    ))}
                  </View>
                  <Text className="text-brand-600 dark:text-brand-400 text-small">
                    ✏️
                  </Text>
                </View>
              )}
            </Pressable>
            <Text className="text-ink-400 dark:text-ink-500 text-micro mt-1">
              標籤跨主分類, 用來標記旅遊 / 出差 / 跟某某人 等情境
            </Text>
          </View>

          {/* Phase 9.1: TagPicker 在 body 內, 跟 detail modal 同層 (它有自己的 Modal) */}
          <TagPicker
            visible={tagPickerVisible}
            value={editTags}
            onChange={setEditTags}
            onClose={() => setTagPickerVisible(false)}
          />

          {/* Phase 9.3 (2026-06-17): 忽略這筆 — 不納入收支統計
              共用 auto_excluded 欄, rule 自動排與使用者手動勾走同一條 stats gate. */}
          <View className="mb-4 mt-1">
            <Pressable
              onPress={() => setEditIgnored((v) => !v)}
              className="flex-row items-center justify-between border border-ink-200 dark:border-ink-700 rounded-xl px-3 py-3 bg-white dark:bg-ink-800 active:bg-ink-50 dark:active:bg-ink-700"
              testID="txn-detail-ignore-toggle"
            >
              <View className="flex-1 mr-3">
                <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold">
                  忽略這筆
                </Text>
                <Text className="text-ink-500 dark:text-ink-400 text-micro mt-0.5">
                  勾起來 → 不納入本月消費 / 收支統計
                </Text>
              </View>
              <View
                className={`w-12 h-7 rounded-full justify-center px-0.5 ${
                  editIgnored ? 'bg-brand-600' : 'bg-ink-300 dark:bg-ink-600'
                }`}
              >
                <View
                  className={`w-6 h-6 rounded-full bg-white shadow-sm ${
                    editIgnored ? 'self-end' : 'self-start'
                  }`}
                />
              </View>
            </Pressable>
          </View>

          {/* Phase 10 (2026-07-29): 分類拆帳 — 一筆拆多類, 每份可獨立決定計不計入統計.
              整筆「忽略這筆」已勾時拆帳無意義 (子項一律被母筆蓋過), 故隱藏. */}
          {!editIgnored && splitParentAmount > 0 ? (
            <SplitEditor
              drafts={editSplits}
              onChange={setEditSplits}
              parentAmount={splitParentAmount}
              categoryOptions={categoryOptions}
              categoriesLoading={categoriesQ.isLoading}
            />
          ) : null}

          {/* Status */}
          {status && (
            <View
              className={`rounded-xl p-3 mb-3 ${
                status.kind === 'ok'
                  ? 'bg-accent-100 dark:bg-accent-950'
                  : 'bg-red-100 dark:bg-red-950'
              }`}
            >
              <Text
                className={`text-small ${
                  status.kind === 'ok'
                    ? 'text-accent-700 dark:text-accent-300'
                    : 'text-red-700 dark:text-red-300'
                }`}
              >
                {status.msg}
              </Text>
            </View>
          )}

          {/* Action buttons */}
          <View className="flex-row gap-2">
            <Pressable
              onPress={onClose}
              className="flex-1 py-3 rounded-xl border border-ink-300 dark:border-ink-600 bg-white dark:bg-ink-800"
            >
              <Text className="text-ink-700 dark:text-ink-300 text-small font-semibold text-center">
                取消
              </Text>
            </Pressable>
            <Pressable
              onPress={() =>
                patchMut.mutate({
                  category: editCat,
                  subcategory: editSub,
                  description_overwrite: editDesc.trim(),
                  tags: editTags,
                  auto_excluded: editIgnored,
                  splits: toApiSplits(editSplits),
                  splitsChanged,
                })
              }
              disabled={patchMut.isPending || !hasChange || splitError !== null}
              className={`flex-1 py-3 rounded-xl bg-brand-600 active:bg-brand-500 ${
                patchMut.isPending || !hasChange || splitError !== null ? 'opacity-40' : ''
              }`}
              testID="txn-detail-save-btn"
            >
              {patchMut.isPending ? (
                <ActivityIndicator color="#fff" size="small" />
              ) : (
                <Text className="text-white text-small font-semibold text-center">
                  儲存
                </Text>
              )}
            </Pressable>
          </View>
    </>
  );

  // iOS pageSheet — 系統自動處理鍵盤 / 永遠不撞 status bar / drag-to-dismiss
  if (Platform.OS === 'ios') {
    return (
      <Modal
        visible={txn !== null}
        animationType="slide"
        presentationStyle="pageSheet"
        onRequestClose={onClose}
      >
        <View className="flex-1 bg-white dark:bg-ink-900">
          <ScrollView
            className="flex-1 px-6 pt-4 pb-6"
            keyboardShouldPersistTaps="handled"
            showsVerticalScrollIndicator={false}
            contentContainerStyle={{ paddingBottom: 32 }}
            automaticallyAdjustKeyboardInsets
          >
            {bodyContent}
          </ScrollView>
        </View>
      </Modal>
    );
  }

  // web/macOS — 中央浮動卡片 (鍵盤不存在不會撞)
  return (
    <Modal visible={txn !== null} transparent animationType="fade" onRequestClose={onClose}>
      <Pressable
        className="flex-1 items-center justify-center bg-black/50 px-4"
        onPress={onClose}
      >
        <Pressable
          className="bg-white dark:bg-ink-900 rounded-2xl w-full max-w-[520px] shadow-card max-h-[90%]"
          onPress={(e) => e.stopPropagation()}
        >
          <ScrollView
            className="p-6"
            keyboardShouldPersistTaps="handled"
            showsVerticalScrollIndicator={false}
          >
            {bodyContent}
          </ScrollView>
        </Pressable>
      </Pressable>
    </Modal>
  );
}

function DetailRow({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <View className="flex-row items-center justify-between py-1.5 border-b border-ink-100 dark:border-ink-800">
      <Text className="text-ink-500 dark:text-ink-400 text-small">{label}</Text>
      <Text
        className={`text-ink-700 dark:text-ink-300 text-small flex-1 ml-4 text-right ${mono ? 'font-mono' : ''}`}
      >
        {value}
      </Text>
    </View>
  );
}
