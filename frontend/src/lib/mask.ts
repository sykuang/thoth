/**
 * Card number / account number display helpers.
 *
 * 為什麼這個 helper 存在:
 *   各家銀行給的卡號格式天差地遠 ── sinopac/ctbc/ubot 給完整 16 碼,
 *   cathay/hsbc/esun/taishin 自帶遮罩 (`9061****7045` / `9059-****-****-7059` /
 *   `9064-XXXX-XXXX-7032` / `0000900000287001`), scsb 給 `A99999****`,
 *   fubon 給 BIN+last4 (`900051******7021`)。
 *
 * (2026-06-13) 拍板 B 方案 ── DB 保留銀行給的原樣 (raw card_no), 顯示時
 * 統一用 `****<末四>` 格式, 確保 UI 視覺一致, 且永遠不會把完整卡號秀給使用者。
 *
 * 規則 (依優先順序):
 *   1. 抽出末 4 連續數字, 顯示 `****<末四>` ── 適用 99% 的卡
 *      `9000000000417020` → `****7020`
 *      `9061****7045` → `****7045`
 *      `9064-XXXX-XXXX-7032` → `****7032`
 *      `9059-****-****-7059` → `****7059`
 *      `A99999****` → `A99999****` (末段就是 mask, 直接回傳原樣)
 *      `0000900000287001` → `****7001` (taishin 全零, 還是 fallback)
 *   2. 無末 4 連續數字 ── 回原字串
 *   3. null/undefined/空字串 ── 回 '—'
 */

export function maskCardNo(s: string | null | undefined): string {
  if (!s) return '—';
  const trimmed = s.trim();
  if (!trimmed) return '—';

  // 取末 4 連續數字 (即使中間混入 - / X / *, 抓字串裡最後的 4 連數)
  const matches = trimmed.match(/\d{4}/g);
  if (!matches || matches.length === 0) {
    // 連 4 連數都沒有 ── 直接回原字串 (e.g. 'A99999****')
    return trimmed;
  }
  const last4 = matches[matches.length - 1];

  // 全零卡號 (taishin 把整段塞成 '0000900000287001') 也吐 ****7001
  // 雖然 last4 是 '7001' 但這就是 backend 端有問題, 顯示如實表達
  return `****${last4}`;
}

/**
 * 帳號 / 卡號通用顯示。account_no 跟 card_no 都吃, 行為一樣。
 * (語意 alias, 讓 caller 程式更易讀)
 */
export const maskAccountOrCard = maskCardNo;
