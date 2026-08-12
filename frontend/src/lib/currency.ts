/**
 * Currency display helper (Phase 6).
 *
 * 統一規則:
 *   - 純台幣 (currency='TWD' 且無 consume_currency) → "+NT$ 1,234" / "-NT$ 567"
 *   - 外幣 billed/pending →
 *       * mode='auto'             "EUR 18.60"  + billed 副字 "NT$ 687"／pending estimate「≈ NT$ 687」
 *       * mode='always_twd'       "+NT$ 687"  (+ 副字 "EUR 18.60")
 *       * mode='always_original'  "EUR 18.60" (純原幣, 無副字)
 *   - HSBC pending (currency=EUR, fx_rate=null) → "EUR 18.60" + 副字 "⏳ 出帳後才有匯率"
 *
 * 設計原則 (符合「禁用推算」鐵令):
 *   - 不在 frontend 算 fx_rate, 全部由 backend 給的 fx_rate field 使用
 *   - fx_rate=null + 外幣 → 必須明示「無匯率資訊」, 不可猜
 *
 * 小數規則:
 *   - TWD: 取整 (台灣銀行系統都是元為單位)
 *   - 外幣 (consume_amount): 保留 2 位小數 (符合大部分國際匯率慣例)
 */
import type { FxDisplayMode, Transaction } from '@/types/api';
import { formatDecimal, formatDecimalFixed } from '@/lib/decimal';

/**
 * 把數字格式化成 "1,234" / "1,234.56" 風格。
 * - 正負號由 caller 自己處理 (本 helper 不放 sign)
 * - 小數位數可控
 */
function formatNumber(n: number, fractionDigits: number = 0): string {
  return n.toLocaleString('zh-TW', {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
}

const CURRENCY_FRACTION_DIGITS: Record<string, number> = {
  TWD: 0,
  JPY: 0,
  KRW: 0,
  USD: 2,
  EUR: 2,
  GBP: 2,
  HKD: 2,
  AUD: 2,
  CAD: 2,
  SGD: 2,
  CHF: 2,
  CNY: 2,
};

function currencyPrefix(currency: string): string {
  const code = currency.trim().toUpperCase();
  return code === 'TWD' ? 'NT$' : code;
}

function currencyFractionDigits(currency: string): number | undefined {
  return CURRENCY_FRACTION_DIGITS[currency.trim().toUpperCase()];
}

/** 格式化單一金額 → "NT$ 1,234" / "EUR 12.34" / "JPY 1,234" */
export function formatCurrency(amount: number, currency: string): string {
  const formatted = formatNumber(Math.abs(amount), currencyFractionDigits(currency) ?? 2);
  const prefix = currencyPrefix(currency);
  return prefix ? `${prefix} ${formatted}` : formatted;
}

/** 顯示 signed numeric 金額；aggregate/KPI 不再各自拼 `$`。 */
export function formatSignedCurrency(
  amount: number,
  currency: string,
  showPositiveSign = false,
): string {
  const sign = amount < 0 ? '-' : showPositiveSign && amount > 0 ? '+' : '';
  return `${sign}${formatCurrency(amount, currency)}`;
}

/**
 * 顯示 exact decimal string，不先轉 Number，保留券商／手動帳戶精度。
 * Invalid decimal returns null so callers can render an honest em dash.
 */
export function formatDecimalCurrency(
  value: string | number,
  currency: string,
): string | null {
  const fractionDigits = currencyFractionDigits(currency);
  const formatted = fractionDigits == null
    ? formatDecimal(String(value))
    : formatDecimalFixed(String(value), fractionDigits);
  if (formatted == null) return null;
  const negative = formatted.startsWith('-');
  const digits = negative ? formatted.slice(1) : formatted;
  const prefix = currencyPrefix(currency);
  return `${negative ? '-' : ''}${prefix ? `${prefix} ` : ''}${digits}`;
}

/** Exact decimal magnitude for account surfaces where color owns direction. */
export function formatAbsoluteDecimalCurrency(
  value: string | number,
  currency: string,
): string | null {
  const magnitude = String(value).trim().replace(/^[+-]/, '');
  return formatDecimalCurrency(magnitude, currency);
}

/** 一個交易渲染結果 — primary 大字, sub 副字, sub 可為 null */
export type AmountRender = {
  /** 大字, 含正負號 (例 "+NT$ 1,234" / "-EUR 18.60") */
  primary: string;
  /** 副字 (例 "NT$ 687"、"≈ NT$ 687" 或 "⏳ 出帳後才有匯率"), null 代表不顯示 */
  sub: string | null;
  /** 給 UI 配色用 — income/expense/zero, caller 自決顏色 class */
  direction: 'income' | 'expense' | 'zero';
};

/**
 * 把 transaction + user mode 變成顯示用的 primary / sub 字串。
 *
 * 對於 backend Transaction shape 的關鍵欄位:
 *   - amount, currency       → 永遠是 row 的「主金額/主幣」(TWD 交易=TWD, HSBC pending=EUR)
 *   - consume_currency,
 *     consume_amount         → 原幣資訊 (外幣消費才有, 純台幣為 null)
 *   - fx_rate                → backend 算好的真實匯率, null 代表無資訊
 *
 * 判斷外幣消費的條件: consume_currency != null && consume_currency != 'TWD'
 *   (避免 HSBC pending 把 currency=EUR 但 consume_currency 也是 EUR 算外幣兩次)
 */
export function renderAmount(txn: Transaction, mode: FxDisplayMode = 'auto'): AmountRender {
  // Phase 6 (B-full): txn_type 決定 direction, 不是純看 amount 符號。
  // 銀行從帳單視角給回饋/退款負值 → 對使用者是正向現金流, 必須顯示綠色正號。
  // payment (還款) 是 transfer, 顯示為中性 (zero), 不算 income 也不算 expense。
  let direction: AmountRender['direction'];
  if (txn.cashflow_direction === 'income' || txn.cashflow_direction === 'expense') {
    direction = txn.cashflow_direction;
  } else if (txn.cashflow_direction === 'neutral') {
    direction = 'zero';
  } else if (txn.txn_type === 'cashback' || txn.txn_type === 'refund' || txn.txn_type === 'fee_waiver') {
    // fee_waiver (年費減免/手續費減免/利息減免): 銀行退還已收費用, 對 user 是 income 方向 (綠色).
    direction = 'income';
  } else if (txn.txn_type === 'payment') {
    direction = 'zero';
  } else {
    direction = txn.amount > 0 ? 'income' : txn.amount < 0 ? 'expense' : 'zero';
  }
  const sign = direction === 'income' ? '+' : direction === 'expense' ? '-' : '';
  const displayAmount = typeof txn.display_amount === 'number'
    ? txn.display_amount
    : Math.abs(txn.amount);

  const isForeign =
    txn.consume_currency != null &&
    txn.consume_currency !== 'TWD' &&
    txn.consume_amount != null &&
    txn.consume_amount !== 0;

  // ============================================================
  // 純台幣 — 不論 mode 一律 "NT$ X"
  // ============================================================
  if (!isForeign) {
    return {
      primary: `${sign}${formatCurrency(displayAmount, txn.currency)}`,
      sub: null,
      direction,
    };
  }

  // ============================================================
  // 外幣消費 — 三種 mode
  // ============================================================
  const originalAbs = Math.abs(txn.consume_amount as number);
  const originalCcy = txn.consume_currency as string;
  const originalStr = `${sign}${formatCurrency(originalAbs, originalCcy)}`;

  // 「主幣」的取得:
  //   - HSBC pending: currency=EUR, amount=662 → row 本身就是原幣計價, 沒 TWD 主金額
  //   - CTBC billed/pending: currency=TWD, amount=2470 → row 主金額是 TWD
  const rowIsTwd = txn.currency === 'TWD';
  const twdAbs = rowIsTwd ? Math.abs(txn.amount) : null;

  switch (mode) {
    case 'always_original':
      // 只顯示原幣, 不副字 (使用者明確說「我不在乎台幣」)
      return { primary: originalStr, sub: null, direction };

    case 'always_twd': {
      // 想看 TWD; 若 row 沒 TWD 主金額 (HSBC pending) → 退化成「無 TWD」hint
      if (twdAbs != null) {
        return {
          primary: `${sign}${formatCurrency(twdAbs, 'TWD')}`,
          sub: `原幣 ${formatCurrency(originalAbs, originalCcy)}`,
          direction,
        };
      }
      // 沒 TWD → 退化成原幣 + hint
      return {
        primary: originalStr,
        sub: '⏳ 出帳後才有台幣金額',
        direction,
      };
    }

    case 'auto':
    default: {
      // MoneyBook 風: 原幣為主, TWD 為副 (若有)
      if (twdAbs != null) {
        return {
          primary: originalStr,
          sub: `${txn.fx_rate_source === 'bank_billed' ? '' : '≈ '}${formatCurrency(twdAbs, 'TWD')}`,
          direction,
        };
      }
      return {
        primary: originalStr,
        sub: '⏳ 出帳後才有台幣金額',
        direction,
      };
    }
  }
}

/**
 * 把 fx_rate_source label 翻成中文人話 (給 detail page 顯示用)。
 * Used by transaction detail UI to disclose rate provenance honestly.
 */
export function fxRateSourceLabel(source: Transaction['fx_rate_source']): string {
  switch (source) {
    case 'bank_billed':
      return '銀行帳單實際匯率 (含跨刷手續費)';
    case 'bank_pending_estimate':
      return '銀行未出帳估算匯率 (出帳前可能變動)';
    default:
      return '';
  }
}

/**
 * 格式化 fx_rate 數字 → "31.6000" (4 位小數讓使用者看細節)。
 * Caller 應該檢查 fx_rate != null 再 call。
 */
export function formatFxRate(rate: number, originalCcy: string): string {
  return `1 ${originalCcy} = ${formatNumber(rate, 4)} TWD`;
}
