/**
 * usePreferences — TanStack Query hook for user preferences.
 *
 * Wraps GET/PUT /users/me/preferences with cache + optimistic update.
 *
 * Usage:
 *   const { data: prefs, mutate } = usePreferences();
 *   const fxMode = prefs?.fx_display_mode ?? 'auto';   // 永遠有 default
 *   mutate({ fx_display_mode: 'always_twd' });
 *
 * Cache strategy:
 *   - staleTime 5 min — preferences 很少改, 不必每次都打 API
 *   - PUT success → 直接用 server 回傳的 merged payload 取代 cache
 *     (backend 已合併 default, 我們不必再合)
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, type ApiError } from '@/lib/api';
import { type UserPreferences } from '@/types/api';

/** Default 跟 backend preferences_router.DEFAULT_PREFERENCES 對齊。 */
export const DEFAULT_PREFERENCES: UserPreferences = {
  fx_display_mode: 'auto',
  card_date_basis: 'consume',
};

export function usePreferences() {
  const qc = useQueryClient();

  const query = useQuery<UserPreferences, ApiError>({
    queryKey: ['user-preferences'],
    queryFn: () => api<UserPreferences>('/users/me/preferences'),
    staleTime: 5 * 60 * 1000, // 5 min
    // 401 unauth 時 api() 已 logout, 這裡不必額外處理
  });

  const mutation = useMutation<UserPreferences, ApiError, Partial<UserPreferences>>({
    mutationFn: (body) =>
      api<UserPreferences>('/users/me/preferences', { method: 'PUT', body }),
    onSuccess: (merged) => {
      // backend 已回 merged payload, 直接 setQueryData 省一次 GET
      qc.setQueryData(['user-preferences'], merged);
    },
  });

  return {
    /** Loading 期間或第一次 fetch 失敗都會吐 default — UI 永遠拿得到值 */
    data: query.data ?? DEFAULT_PREFERENCES,
    isLoading: query.isLoading,
    error: query.error,
    mutate: mutation.mutate,
    isMutating: mutation.isPending,
    mutationError: mutation.error,
  };
}
