import type { SnapTradeStatus } from '../types/api';
import { multiplyDecimalExact } from './decimal';

/** SnapTrade average_cost is per share; other assets need an unavailable multiplier. */
export function holdingTotalCost(position: {
  asset_type?: string | null; average_cost?: string | null; quantity?: string | null;
}): string | null {
  if (!['CS', 'STOCK', 'EQUITY', 'ETF', 'ET'].includes(position.asset_type?.trim().toUpperCase() ?? '')) return null;
  if (typeof position.average_cost !== 'string' || typeof position.quantity !== 'string') return null;
  const quantity = /^([+-]?)(\d+(?:\.\d+)?)$/.exec(position.quantity.trim());
  if (!quantity) return null;
  const cost = multiplyDecimalExact(position.average_cost, quantity[2]);
  return cost == null ? null : quantity[1] === '-' && cost !== '0' ? `-${cost}` : cost;
}

export function formatSnapTradeConnectionStatus(status: SnapTradeStatus): string {
  if (!status.configured) return '伺服器尚未設定 SnapTrade';
  if (!status.registered) return '尚未開始連結';
  if (!status.connections) return '連線狀態未知，請重新整理';
  if (!status.connections.length) return '已建立 SnapTrade 使用者，尚未連結券商';
  const active = status.connections.filter((connection) => connection.disabled === false).length;
  const disabled = status.connections.filter((connection) => connection.disabled === true).length;
  const unknown = status.connections.length - active - disabled;
  return `有效 ${active} 個 · 待修復 ${disabled} 個 · 狀態未知 ${unknown} 個`;
}

export function shouldSyncAfterSnapTradePortal(
  result: string, status: SnapTradeStatus | undefined, reconnect?: string,
): boolean {
  const connections = status?.connections;
  if (result !== 'success' || !connections?.length) return false;
  return connections.every((connection) => connection.disabled === false)
    && (!reconnect || connections.some((connection) => connection.id === reconnect));
}

const TRANSPORT_ERROR_MARKERS = [
  'fetch failed',
  'failed to fetch',
  'network request failed',
  'network connection was lost',
  'load failed',
];

export function formatSnapTradeUiError(error: unknown, message: string, hasSnapshot: boolean): string {
  const status = error && typeof error === 'object' && 'status' in error
    && typeof error.status === 'number' ? error.status : null;
  const nativeTransportError = status == null && error instanceof Error
    && TRANSPORT_ERROR_MARKERS.some((marker) => error.message.toLowerCase().includes(marker));
  if (status !== 0 && !nativeTransportError) return message;
  return hasSnapshot
    ? '同步連線中斷，資料未更新；目前顯示上次成功同步的快照。請稍後重試。'
    : '暫時無法連線伺服器，請稍後重試。';
}
