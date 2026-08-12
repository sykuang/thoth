import { Platform } from 'react-native';

import {
  parsePushProvider,
  supportsDevicePush,
  type PushProvider,
  type PushRegistrationResult,
} from './pushProvider';

const PUSH_PROVIDER = parsePushProvider(process.env.EXPO_PUBLIC_PUSH_PROVIDER);

type NotificationHandlers = {
  onNotificationReceived?: (data: Record<string, unknown>) => void;
  onNotificationTap?: (data: Record<string, unknown>) => void;
};

/** Native-only implementation is loaded lazily so Web/SSR never evaluates expo-notifications. */
export async function registerForPushNotifications(): Promise<string | null> {
  if (Platform.OS === 'web' || !supportsDevicePush(PUSH_PROVIDER)) return null;
  try {
    const native = await import('./pushNative');
    return await native.registerForPushNotificationsNative(PUSH_PROVIDER);
  } catch (error) {
    console.warn('[push] native module unavailable:', error);
    return null;
  }
}

export async function requestPushNotifications(): Promise<PushRegistrationResult> {
  if (Platform.OS === 'web' || !supportsDevicePush(PUSH_PROVIDER)) return 'unavailable';
  const native = await import('./pushNative');
  return native.requestPushNotificationsNative(PUSH_PROVIDER);
}

export async function getPushPermissionStatus(): Promise<'granted' | 'denied' | 'undetermined' | 'unavailable'> {
  if (Platform.OS === 'web' || !supportsDevicePush(PUSH_PROVIDER)) return 'unavailable';
  try {
    const native = await import('./pushNative');
    return await native.getPushPermissionStatusNative();
  } catch {
    return 'unavailable';
  }
}

/**
 * Attach native notification listeners with race-safe async module loading.
 * Web/Tauri returns a synchronous no-op cleanup and has no native import side effects.
 */
export function attachNotificationListeners(opts: NotificationHandlers): () => void {
  if (Platform.OS === 'web' || !supportsDevicePush(PUSH_PROVIDER)) return () => undefined;

  let closed = false;
  let detach: () => void = () => undefined;
  void import('./pushNative')
    .then((native) => {
      if (closed) return;
      detach = native.attachNotificationListenersNative(opts);
    })
    .catch((error) => {
      console.warn('[push] native module unavailable:', error);
    });

  return () => {
    closed = true;
    detach();
  };
}

export function getPushProvider(): PushProvider {
  return PUSH_PROVIDER;
}
