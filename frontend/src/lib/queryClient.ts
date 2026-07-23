/**
 * Singleton react-query client.
 *
 * Exported separately from `_layout.tsx` so that non-component modules
 * (e.g. `stores/auth.ts`, `lib/api.ts`) can call `queryClient.clear()`
 * when the user logs in/out or the token gets revoked.
 *
 * **Phase C-fe (2026-06-17)** — cross-user cache leak fix:
 *   Pre-fix: queryClient was module-private in `_layout.tsx`. When user A
 *   logged out and user B logged in on the same device, react-query's
 *   30s `staleTime` cache (transactions/cards/portfolio/me/...) was
 *   never cleared → user B briefly saw user A's data.
 *   Post-fix: every auth transition (login success, logout, 401 from
 *   server, biometric reject) calls `queryClient.clear()` synchronously
 *   before navigating to the next screen.
 *
 * **Phase X (2026-06-18)** — traffic reduction:
 *   Default staleTime bumped from 30s → 5min and gcTime added (30min)
 *   so user can tab-switch / background-foreground freely without
 *   triggering refetch storms on the same data. Real-time data (sync
 *   polling, transactions list, cards list, rules) overrides per-query
 *   to keep their tighter freshness budget. All mutations that touch
 *   relevant data already call invalidateQueries, so cache freshness
 *   is correctness-preserving, not lazy.
 *
 *   Why not Cloudflare cache instead: every endpoint is JWT-bound and
 *   per-user, so CF can't safely cache. Real fix is client-side cache,
 *   not edge cache.
 */
import { QueryClient } from '@tanstack/react-query';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      // 5 minutes — most thoth data (accounts, portfolio summary, stats,
      // me) is sync-driven, not real-time. Mutations invalidate explicitly.
      // Per-query overrides for real-time data (sync polling) below.
      staleTime: 5 * 60 * 1000,
      // 30 minutes — keep inactive query data in memory long enough that
      // tab-switching / app foregrounding doesn't pay re-fetch cost.
      // Defaults to 5min which is too aggressive for mobile.
      gcTime: 30 * 60 * 1000,
    },
  },
});
