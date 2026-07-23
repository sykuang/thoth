type NotificationData = Record<string, unknown>;

/**
 * Map push-notification payloads to routes that actually exist in Expo Router.
 *
 * Remote providers may include a raw custom-scheme URL (for example `thoth:///`)
 * when a notification opens the app. Expo Router treats that URL as a route and
 * shows Unmatched Route unless we translate notification metadata ourselves.
 */
export function routeFromNotificationData(data: NotificationData): string | null {
  const kind = typeof data.kind === 'string' ? data.kind : '';
  const explicit = normalizeInAppRoute(data.deep_link);

  if (explicit && explicit !== '/' && isKnownNotificationRoute(explicit)) {
    return explicit;
  }

  switch (kind) {
    case 'payment_reminder':
    case 'new_bill':
    case 'new_payment':
      return '/(tabs)/cards';
    case 'sync_done':
    case 'sync_failed':
    case 'sync_all_done':
    case 'sync_all_failed':
      return '/(tabs)/cards';
    default:
      return explicit ?? null;
  }
}

function normalizeInAppRoute(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const raw = value.trim();
  if (!raw) return null;

  if (raw.startsWith('/')) return raw;

  try {
    const url = new URL(raw, 'thoth://app');
    if (url.protocol !== 'thoth:') return null;

    const path = normalizePathFromCustomScheme(url);
    return `${path}${url.search}${url.hash}`;
  } catch {
    return null;
  }
}

function normalizePathFromCustomScheme(url: URL): string {
  const host = url.hostname;
  const pathname = url.pathname || '';

  if (!host) return pathname || '/';
  if (host === 'app') return pathname || '/';

  return `/${host}${pathname}`;
}

function isKnownNotificationRoute(route: string): boolean {
  const path = route.split(/[?#]/, 1)[0] ?? route;
  return (
    path === '/' ||
    path === '/(tabs)/dashboard' ||
    path === '/(tabs)/cards' ||
    path === '/(tabs)/transactions' ||
    path === '/(tabs)/settings'
  );
}
