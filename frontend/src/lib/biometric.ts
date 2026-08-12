/**
 * Biometric (Touch/Face ID) helper — iOS native only.
 *
 * Web 上：biometricAvailable() 永遠回 false、authenticate() 直接 return true，
 * 維持 Phase 3 web flow 不變（不會被卡解鎖框）。
 */
import * as LocalAuthentication from 'expo-local-authentication';
import { Platform } from 'react-native';

export async function biometricAvailable(): Promise<boolean> {
  if (Platform.OS === 'web') return false;
  const has = await LocalAuthentication.hasHardwareAsync();
  const enrolled = await LocalAuthentication.isEnrolledAsync();
  return has && enrolled;
}

export async function authenticate(reason = '解鎖 Thoth'): Promise<boolean> {
  if (Platform.OS === 'web') return true; // web bypass
  const result = await LocalAuthentication.authenticateAsync({
    promptMessage: reason,
    cancelLabel: '取消',
    fallbackLabel: '使用密碼',
    disableDeviceFallback: false,
  });
  return result.success;
}
