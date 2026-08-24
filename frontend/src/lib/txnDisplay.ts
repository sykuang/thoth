/**
 * Transaction display helpers — pure functions shared by row + modal.
 *
 * Extracted from app/(tabs)/transactions.tsx (W Phase 17, 2026-06-17)
 * to allow TxnRow + TxnDetailModal to live in their own files without
 * cross-importing the screen module.
 */

import { maskCardNo } from './mask';

/**
 * 交易明細來源副標：銀行名稱 + 帳號／卡號。
 * 存款帳戶顯示完整 canonical `account_no`；信用卡直接顯示末四碼（不加遮罩符號）。
 * 缺少來源號碼時保留既有銀行名稱，避免顯示多餘分隔符。
 */
export function formatTransactionSource(
  bankLabel: string,
  source: {
    kind: 'twd' | 'billed' | 'pending';
    accountNo: string | null | undefined;
    accountOrCard: string | null | undefined;
  },
): string {
  const accountNo = source.kind === 'twd' ? source.accountNo?.trim() : null;
  if (accountNo) return `${bankLabel} - ${accountNo}`;

  const masked = maskCardNo(source.accountOrCard);
  if (masked === '—') return bankLabel;
  const cardLastFour = masked.startsWith('****') ? masked.slice(4) : masked;
  return `${bankLabel} - ${cardLastFour}`;
}

/**
 * Phase 7.5 (2026-06-15 使用者指示): MoneyBook-style row 左欄日期堆疊.
 * 把 "2026-06-12" 拆成 { month: "6月", day: "12" } 兩段顯示.
 * 若 date 無效或缺失, 回 ('—', '—') 不 crash.
 */
export function parseDateForMobileLayout(date: string | null | undefined): {
  month: string;
  day: string;
} {
  if (!date) return { month: '—', day: '—' };
  // 容錯各種格式: "2026-06-12" / "2026/06/12" / "2026-06-12T10:30:00"
  const m = date.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})/);
  if (!m) return { month: '—', day: '—' };
  return {
    month: `${parseInt(m[2], 10)}月`,
    day: m[3].padStart(2, '0'),
  };
}

/**
 * Phase 8.2 (2026-06-14): 取顯示用的說明 — overwrite 優先.
 * DB persists the canonical description (bank description + memo); frontend only chooses
 * overwrite vs canonical DB value and must not reconstruct classification text.
 * 回傳 [shown_text, is_overwritten] — caller 可加 ✏️ 標記讓使用者看到這是覆寫.
 */
export function getDisplayDescription(
  t: {
    description: string | null;
    description_overwrite?: string | null;
    display_description?: string | null;
    memo?: string | null; // carried by Transaction for audit; never joined in UI
  }
): [string, boolean] {
  const overwrite = t.description_overwrite;
  if (overwrite && overwrite.length > 0) return [overwrite, true];
  return [(t.display_description || t.description || '').trim() || '—', false];
}

/**
 * Phase 7.5 (2026-06-15 使用者指示): scope (信用卡 pending 子範圍) 中文化.
 * backend 寫 unbilled (未出帳) / realtime (即時) 兩 enum, UI 顯示中文.
 */
export const SCOPE_LABEL: Record<string, string> = {
  unbilled: '未出帳',
  realtime: '即時刷卡',
};
