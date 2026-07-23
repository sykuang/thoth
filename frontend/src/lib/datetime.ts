/**
 * Format an ISO 8601 UTC timestamp into the device's local time string.
 *
 * 把 backend 來的 ISO 8601 UTC 時間（如 `2026-06-13T18:18:51.000Z`）
 * 用瀏覽器 / 裝置當下時區顯示成易讀文字。
 *
 * Backend stores all timestamps as UTC ISO 8601 (Phase 6 後)。Frontend 用
 * `new Date()` 解析含 `Z` 字串會自動視為 UTC，再由 `toLocaleString` 轉成
 * device timezone（使用者台灣 → UTC+8 → 顯示 2026-06-14 02:18）。
 *
 * Legacy fallback: 老 row 在 backend migration 跑過後也會被補成 `Z`，
 * 但若萬一遇到沒 `Z` 的字串，這 helper 會「強制當 UTC」處理，避免被
 * `new Date()` 當 local 多 8 hr。
 *
 * @param iso  ISO 8601 timestamp，例如 `2026-06-13T18:18:51.000Z`。null/空字串會回 `'-'`。
 * @param opts.dateOnly  true → 只顯示日期；false (預設) → 日期+時間。
 * @returns 顯示字串，例如 `2026/06/14 02:18` 或 `'-'`。
 *
 * @example
 *   formatLocalDateTime('2026-06-13T18:18:51.000Z')        // '2026/06/14 02:18' (UTC+8)
 *   formatLocalDateTime('2026-06-13 18:18:51')             // 同上 (legacy 自動補 Z)
 *   formatLocalDateTime(null)                               // '-'
 *   formatLocalDateTime(iso, { dateOnly: true })            // '2026/06/14'
 */
export function formatLocalDateTime(
  iso: string | null | undefined,
  opts?: { dateOnly?: boolean },
): string {
  if (!iso) return '-';

  // Legacy fallback: 老格式 'YYYY-MM-DD HH:MM:SS' 沒 'T'/'Z' → 強制當 UTC
  // （避免被 new Date 當 local 時間多 8 hr）
  let normalized = iso;
  if (!iso.includes('T') && !iso.includes('Z')) {
    normalized = iso.replace(' ', 'T') + 'Z';
  } else if (!iso.endsWith('Z') && !iso.match(/[+-]\d{2}:?\d{2}$/)) {
    // 有 'T' 沒 'Z' 也沒 ±HH:MM → 補 Z 視為 UTC
    normalized = iso + 'Z';
  }

  const d = new Date(normalized);
  if (Number.isNaN(d.getTime())) return iso; // parse 失敗 → 原樣回傳

  return d.toLocaleString('zh-TW', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    ...(opts?.dateOnly
      ? {}
      : { hour: '2-digit', minute: '2-digit', hour12: false }),
  });
}

/**
 * Format an ISO timestamp as "N 分鐘 / 小時 / 天前" relative to now.
 *
 * MoneyBook 風: 「2 小時前」「9 天前」。比絕對時間更直覺，適合 last sync time。
 *
 * @param iso  ISO 8601 timestamp。null/空字串會回 `'從未'`。
 * @returns relative time 字串，例如 `'2 小時前'`, `'剛剛'`, `'從未'`。
 */
export function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return '從未';
  let normalized = iso;
  if (!iso.includes('T') && !iso.includes('Z')) {
    normalized = iso.replace(' ', 'T') + 'Z';
  } else if (!iso.endsWith('Z') && !iso.match(/[+-]\d{2}:?\d{2}$/)) {
    normalized = iso + 'Z';
  }
  const d = new Date(normalized);
  if (Number.isNaN(d.getTime())) return iso;

  const diffMs = Date.now() - d.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 0) return '剛剛'; // 未來時間（時鐘漂移）→ 視為剛剛
  if (diffSec < 60) return '剛剛';
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin} 分鐘前`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr} 小時前`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 30) return `${diffDay} 天前`;
  const diffMon = Math.floor(diffDay / 30);
  if (diffMon < 12) return `${diffMon} 個月前`;
  const diffYr = Math.floor(diffMon / 12);
  return `${diffYr} 年前`;
}
