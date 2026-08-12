import { parsePushProvider, supportsDevicePush } from './pushProvider';

function assertEqual<T>(actual: T, expected: T): void {
  if (actual !== expected) throw new Error(`expected ${String(expected)}, got ${String(actual)}`);
}

assertEqual(parsePushProvider(undefined), 'expo');
assertEqual(parsePushProvider(' APNS '), 'apns');
assertEqual(parsePushProvider('webhook'), 'webhook');
assertEqual(parsePushProvider('typo'), 'none');
assertEqual(supportsDevicePush('expo'), true);
assertEqual(supportsDevicePush('webhook'), false);
assertEqual(supportsDevicePush('none'), false);

console.log('push provider tests passed');
