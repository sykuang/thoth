/**
 * Bank brand metadata — color tokens + short labels for UI.
 *
 * 來源: 各銀行 2024-2026 官網品牌色 (從 .css 或 Logo 取色).
 * 用於: BankBadge + AccountCard 點綴色 + transactions chip border.
 *
 * 設計取捨:
 *   - 沒有引入 11 個 PNG 圖檔 (avoid bundle bloat + 銀行 logo 著作權問題);
 *     改用「真實 brand color + 中文兩字縮寫」做純 View badge.
 *   - dark mode 不另設色 (brand color 本身對比夠強, 套灰底也明顯).
 *
 * SupportedBank 的順序對齊 backend SUPPORTED_BANKS, 不對齊就 TypeScript 噴錯.
 */
import type { SupportedBank } from '@/types/api';

export type BankMeta = {
  /** 中文兩字縮寫, 顯示在 badge 內 (避免英文字母縮寫不直觀). */
  short: string;
  /** Hex color (#RRGGBB), 來自官網 brand spec. badge 背景色用. */
  color: string;
  /** Badge 內字色 ('white' / 'black'), 跟 color 對比足夠的選一個. */
  fg: 'white' | 'black';
  /** 帳戶 type 主要走向 — credit_card / deposit / mixed (Dashboard 分區用) */
  primary: 'credit_card' | 'deposit' | 'mixed';
};

export const BANK_META: Record<SupportedBank, BankMeta> = {
  cathay:   { short: '國泰', color: '#00665e', fg: 'white', primary: 'mixed' },         // 國泰深綠
  ctbc:     { short: '中信', color: '#003a8c', fg: 'white', primary: 'mixed' },         // 中信藍
  dbs:      { short: '星展', color: '#e30613', fg: 'white', primary: 'mixed' },         // DBS red
  esun:     { short: '玉山', color: '#1e7a3c', fg: 'white', primary: 'mixed' },         // 玉山綠
  fubon:    { short: '富邦', color: '#00754a', fg: 'white', primary: 'mixed' },         // 富邦綠
  hsbc:     { short: '滙豐', color: '#db0011', fg: 'white', primary: 'credit_card' },   // HSBC red
  linebank: { short: 'LINE', color: '#06c755', fg: 'white', primary: 'deposit' },       // LINE green
  rakuten:  { short: '樂天', color: '#bf0000', fg: 'white', primary: 'deposit' },       // Rakuten red
  scb:      { short: '渣打', color: '#0473ea', fg: 'white', primary: 'mixed' },         // 渣打藍
  scsb:     { short: '上海', color: '#a2231d', fg: 'white', primary: 'mixed' },         // 上海商銀 (百年實體綜合, 1915 創立, 非數位)
  sinopac:  { short: '永豐', color: '#005bac', fg: 'white', primary: 'mixed' },         // 永豐藍
  taishin:  { short: '台新', color: '#0e4e96', fg: 'white', primary: 'mixed' },         // 台新藍
  ubot:     { short: '聯邦', color: '#c8161e', fg: 'white', primary: 'mixed' },         // 聯邦紅 (使用者有存款帳戶 + 信用卡)
};

/** 安全 lookup — bank 不在 BANK_META 時 fallback 中性灰 badge. */
export function bankMeta(bank: string): BankMeta {
  return BANK_META[bank as SupportedBank] ?? {
    short: bank.slice(0, 2).toUpperCase(),
    color: '#64748b',  // slate-500
    fg: 'white',
    primary: 'mixed',
  };
}
