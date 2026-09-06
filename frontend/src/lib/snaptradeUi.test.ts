import { formatSnapTradeUiError, formatSnapTradeConnectionStatus, shouldSyncAfterSnapTradePortal } from './snaptradeUi';
import type { SnapTradeStatus } from '../types/api';

function assertEqual(actual: string, expected: string): void {
  if (actual !== expected) throw new Error(`expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

const nativeNetworkError = new Error('fetch failed: The network connection was lost.');
assertEqual(
  formatSnapTradeUiError(nativeNetworkError, '連線失敗: fetch failed: The network connection was lost.', true),
  '同步連線中斷，資料未更新；目前顯示上次成功同步的快照。請稍後重試。',
);

const timeoutError = Object.assign(new Error('timeout'), { status: 0, body: { detail: '請求超過 120000ms 未回應' } });
assertEqual(
  formatSnapTradeUiError(timeoutError, '請求超過 120000ms 未回應', false),
  '暫時無法連線伺服器，請稍後重試。',
);

const apiError = Object.assign(new Error('HTTP 502'), {
  status: 502,
  body: { detail: 'SnapTrade API 回報：連線失敗，請重新授權' },
});
assertEqual(
  formatSnapTradeUiError(apiError, 'SnapTrade API 回報：連線失敗，請重新授權', true),
  'SnapTrade API 回報：連線失敗，請重新授權',
);

const registered: SnapTradeStatus = {
  configured: true, registered: true, connection_count: 2, last_synced_at: null,
};
assertEqual(formatSnapTradeConnectionStatus(registered), '連線狀態未知，請重新整理');
assertEqual(formatSnapTradeConnectionStatus({ ...registered, connections: [
  { id: 'active', brokerage_name: 'Active Broker', disabled: false },
  { id: 'disabled', brokerage_name: 'Disabled Broker', disabled: true },
  { id: null, brokerage_name: null, disabled: null },
] }), '有效 1 個 · 待修復 1 個 · 狀態未知 1 個');
assertEqual(formatSnapTradeConnectionStatus({ ...registered, connections: [
  { id: 'disabled', brokerage_name: null, disabled: true },
] }), '有效 0 個 · 待修復 1 個 · 狀態未知 0 個');
assertEqual(formatSnapTradeConnectionStatus({ ...registered, connections: [] }), '已建立 SnapTrade 使用者，尚未連結券商');
assertEqual(formatSnapTradeConnectionStatus({ ...registered, registered: false }), '尚未開始連結');
assertEqual(formatSnapTradeConnectionStatus({ ...registered, configured: false }), '伺服器尚未設定 SnapTrade');

const repaired: SnapTradeStatus = { ...registered, connections: [
  { id: 'auth-repair', brokerage_name: 'Synthetic Broker', disabled: false },
] };
for (const result of ['cancel', 'dismiss', 'locked']) {
  if (shouldSyncAfterSnapTradePortal(result, repaired, 'auth-repair')) throw new Error('Cancelled portal must not sync');
}
for (const status of [undefined, registered, { ...repaired, connections: [] }, {
  ...repaired, connections: [{ ...repaired.connections![0], disabled: true }],
}, {
  ...repaired, connections: [{ ...repaired.connections![0], disabled: null }],
}, {
  ...repaired, connections: [...repaired.connections!, { id: 'other', brokerage_name: null, disabled: true }],
}]) {
  if (shouldSyncAfterSnapTradePortal('success', status, 'auth-repair')) throw new Error('Callback is not proof of active connections');
}
if (shouldSyncAfterSnapTradePortal('success', repaired, 'missing')) throw new Error('Missing repair target is not repaired');
if (!shouldSyncAfterSnapTradePortal('success', repaired, 'auth-repair')) throw new Error('Verified repair should sync');
if (!shouldSyncAfterSnapTradePortal('success', repaired)) throw new Error('Normal connection should still sync');

console.log('SnapTrade UI tests passed');
