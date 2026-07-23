// Period (day/month/year) granularity helpers — 從 transactions.tsx 拆出。
// 為什麼用 helper 寫週期計算: transactions, dashboard, reports 都會用到，
// 集中一處避免 logic drift; 每函式都是 pure，方便 test。

export type Granularity = 'day' | 'month' | 'year';

/** 取當下 granularity 的 period key，e.g. 'day' -> '2026-06-17', 'month' -> '2026-06' */
export function currentPeriodKey(g: Granularity): string {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  if (g === 'day') return `${y}-${m}-${dd}`;
  if (g === 'year') return `${y}`;
  return `${y}-${m}`;
}

/** 取某 period 的 since/until ISO date 範圍 (含頭含尾) */
export function periodRange(g: Granularity, key: string): { since: string; until: string } {
  if (g === 'day') {
    return { since: key, until: key };
  }
  if (g === 'year') {
    return { since: `${key}-01-01`, until: `${key}-12-31` };
  }
  // month
  const [yStr, mStr] = key.split('-');
  const y = parseInt(yStr, 10);
  const m = parseInt(mStr, 10);
  const lastDay = new Date(y, m, 0).getDate();
  return {
    since: `${key}-01`,
    until: `${key}-${String(lastDay).padStart(2, '0')}`,
  };
}

/** key 推進 delta 期（正/負），回傳新 key */
export function shiftPeriod(g: Granularity, key: string, delta: number): string {
  if (g === 'day') {
    const [y, m, d] = key.split('-').map((s) => parseInt(s, 10));
    const next = new Date(y, m - 1, d + delta);
    return `${next.getFullYear()}-${String(next.getMonth() + 1).padStart(2, '0')}-${String(next.getDate()).padStart(2, '0')}`;
  }
  if (g === 'year') {
    return String(parseInt(key, 10) + delta);
  }
  // month
  const [yStr, mStr] = key.split('-');
  const y = parseInt(yStr, 10);
  const m = parseInt(mStr, 10);
  const next = new Date(y, m - 1 + delta, 1);
  return `${next.getFullYear()}-${String(next.getMonth() + 1).padStart(2, '0')}`;
}

/** UI label，e.g. month + 2026-06 + 本年 -> "6 月"; 跨年 -> "2025 年 12 月" */
export function periodDisplayLabel(g: Granularity, key: string): string {
  const now = new Date();
  if (g === 'day') {
    const [y, m, d] = key.split('-').map((s) => parseInt(s, 10));
    const isThisYear = y === now.getFullYear();
    return isThisYear ? `${m}/${d}` : `${y}/${m}/${d}`;
  }
  if (g === 'year') {
    return `${key} 年`;
  }
  // month
  const [yStr, mStr] = key.split('-');
  const isThisYear = parseInt(yStr, 10) === now.getFullYear();
  return isThisYear
    ? `${parseInt(mStr, 10)} 月`
    : `${yStr} 年 ${parseInt(mStr, 10)} 月`;
}
