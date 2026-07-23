import { routeFromNotificationData } from '@/lib/pushRoutes';

/**
 * Rewrite native launch URLs before Expo Router treats them as file-system routes.
 *
 * iOS may launch Thoth from a notification with only the app scheme (`thoth:///`).
 * Without this guard Expo Router tries to open that literal URL and displays
 * "Unmatched Route" before our notification listener can navigate.
 */
export function redirectSystemPath({ path }: { path: string; initial: boolean }): string {
  try {
    if (path === 'thoth:///' || path === 'thoth://' || path === 'thoth:') {
      return '/';
    }

    const route = routeFromNotificationData({ deep_link: path });
    return route ?? '/';
  } catch {
    return '/';
  }
}
