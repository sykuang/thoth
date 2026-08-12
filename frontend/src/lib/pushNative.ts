import Constants from 'expo-constants';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';
import { Platform } from 'react-native';

import { api } from './api';
import type { DevicePushProvider, PushRegistrationResult } from './pushProvider';

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldPlaySound: true,
    shouldSetBadge: true,
    shouldShowBanner: true,
    shouldShowList: true,
  }),
});

async function ensureAndroidNotificationChannel(): Promise<void> {
  if (Platform.OS !== 'android') return;
  await Notifications.setNotificationChannelAsync('default', {
    name: '一般通知',
    importance: Notifications.AndroidImportance.DEFAULT,
  });
}

export async function registerForPushNotificationsNative(
  provider: DevicePushProvider,
): Promise<string | null> {
  if (!Device.isDevice) return null;

  try {
    const { status } = await Notifications.getPermissionsAsync();
    if (status !== 'granted') return null;
    await ensureAndroidNotificationChannel();

    let token: string;
    if (provider === 'apns' || provider === 'fcm') {
      token = (await Notifications.getDevicePushTokenAsync()).data;
    } else {
      const projectId = Constants.expoConfig?.extra?.eas?.projectId ?? Constants.easConfig?.projectId;
      if (!projectId) return null;
      token = (await Notifications.getExpoPushTokenAsync({ projectId })).data;
    }
    if (!token) return null;

    await api('/me/push-tokens', {
      method: 'PUT',
      body: {
        provider,
        token,
        platform: Platform.OS === 'ios' ? 'ios' : 'android',
        device_label: Device.modelName ?? 'Unknown device',
      },
    });
    return token;
  } catch (error) {
    console.warn('[push] registration failed:', error);
    return null;
  }
}

export async function requestPushNotificationsNative(
  provider: DevicePushProvider,
): Promise<PushRegistrationResult> {
  if (!Device.isDevice) return 'unavailable';
  const existing = await Notifications.getPermissionsAsync();
  if (existing.status !== 'granted') await ensureAndroidNotificationChannel();
  const status = existing.status === 'granted'
    ? existing.status
    : (await Notifications.requestPermissionsAsync()).status;
  if (status !== 'granted') return 'permission_denied';
  const token = await registerForPushNotificationsNative(provider);
  return token ? 'registered' : 'registration_failed';
}

export async function getPushPermissionStatusNative(): Promise<'granted' | 'denied' | 'undetermined' | 'unavailable'> {
  if (!Device.isDevice) return 'unavailable';
  const { status } = await Notifications.getPermissionsAsync();
  return status === 'granted' || status === 'denied' ? status : 'undetermined';
}

export function attachNotificationListenersNative(opts: {
  onNotificationReceived?: (data: Record<string, unknown>) => void;
  onNotificationTap?: (data: Record<string, unknown>) => void;
}): () => void {
  const subscriptions: Notifications.EventSubscription[] = [];
  let cancelled = false;

  const handleResponse = (response: Notifications.NotificationResponse) => {
    const data = response.notification.request.content.data ?? {};
    try {
      opts.onNotificationTap?.(data as Record<string, unknown>);
    } catch (error) {
      console.warn('[push] tap handler error:', error);
    }
  };

  if (opts.onNotificationReceived) {
    subscriptions.push(Notifications.addNotificationReceivedListener((notification) => {
      const data = notification.request.content.data ?? {};
      try {
        opts.onNotificationReceived?.(data as Record<string, unknown>);
      } catch (error) {
        console.warn('[push] receive handler error:', error);
      }
    }));
  }

  if (opts.onNotificationTap) {
    void Notifications.getLastNotificationResponseAsync()
      .then((response) => {
        if (!cancelled && response) {
          handleResponse(response);
          void Notifications.clearLastNotificationResponseAsync?.().catch(() => undefined);
        }
      })
      .catch((error) => {
        console.warn('[push] get last notification response failed:', error);
      });

    subscriptions.push(Notifications.addNotificationResponseReceivedListener(handleResponse));
  }

  return () => {
    cancelled = true;
    subscriptions.forEach((subscription) => subscription.remove());
  };
}
