import type {
  BankAccount,
  DashboardStats,
  PortfolioSummary,
} from '@/types/api';

import type { ReplicaDashboardCache } from './replica';

type DashboardCacheFetchers = {
  accounts: () => Promise<BankAccount[]>;
  portfolio: () => Promise<PortfolioSummary>;
  stats: () => Promise<DashboardStats>;
  now?: () => string;
};

type DashboardRevisionTuple = [number, number, number];

export function hasNewerDashboardRevision(
  current: DashboardRevisionTuple,
  complete: DashboardRevisionTuple,
): boolean {
  return current.some((revision, index) => revision > complete[index]);
}

export async function fetchCompleteDashboardCache({
  accounts,
  portfolio,
  stats,
  now = () => new Date().toISOString(),
}: DashboardCacheFetchers): Promise<ReplicaDashboardCache> {
  const [accountRows, portfolioSummary, dashboardStats] = await Promise.all([
    accounts(),
    portfolio(),
    stats(),
  ]);
  return {
    cachedAt: now(),
    accounts: accountRows,
    portfolio: portfolioSummary,
    stats: dashboardStats,
  };
}
