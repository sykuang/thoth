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
