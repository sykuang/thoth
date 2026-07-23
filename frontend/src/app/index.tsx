/**
 * Root index — redirect based on auth state.
 *
 * If logged in → dashboard tabs; else → /login.
 */
import { Redirect } from 'expo-router';

import { useAuthStore } from '@/stores/auth';

export default function Index() {
  const token = useAuthStore((s) => s.token);
  if (token) {
    return <Redirect href="/(tabs)/dashboard" />;
  }
  return <Redirect href="/login" />;
}
