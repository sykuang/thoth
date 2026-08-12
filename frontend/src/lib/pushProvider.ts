export type PushProvider = 'apns' | 'webhook' | 'fcm' | 'expo' | 'none';
export type DevicePushProvider = Extract<PushProvider, 'apns' | 'fcm' | 'expo'>;
export type PushRegistrationResult =
  | 'registered'
  | 'permission_denied'
  | 'registration_failed'
  | 'unavailable';

const PUSH_PROVIDERS = new Set<PushProvider>(['apns', 'webhook', 'fcm', 'expo', 'none']);

export function parsePushProvider(value: string | undefined): PushProvider {
  const normalized = (value ?? 'expo').trim().toLowerCase();
  return PUSH_PROVIDERS.has(normalized as PushProvider) ? normalized as PushProvider : 'none';
}

export function supportsDevicePush(provider: PushProvider): provider is DevicePushProvider {
  return provider === 'apns' || provider === 'fcm' || provider === 'expo';
}
