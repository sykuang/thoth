import { formatSnapTradeUiError } from './snaptradeUi';

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

console.log('SnapTrade UI error tests passed');
