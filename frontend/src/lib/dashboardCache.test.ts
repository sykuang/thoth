import {
  fetchCompleteDashboardCache,
  hasNewerDashboardRevision,
} from './dashboardCache';

function equal(actual: unknown, expected: unknown): void {
  if (!Object.is(actual, expected)) throw new Error(`expected ${String(expected)}, got ${String(actual)}`);
}

async function main() {
  let releaseStats!: (value: { total: number }) => void;
  const statsGate = new Promise<{ total: number }>((resolve) => { releaseStats = resolve; });
  let settled = false;
  const complete = fetchCompleteDashboardCache({
    accounts: async () => [{ has_creds: true }] as never,
    portfolio: async () => ({ current_month_spending: 9479 }) as never,
    stats: async () => statsGate as never,
    now: () => '2026-08-10T00:00:00Z',
  });
  void complete.then(() => { settled = true; });
  await Promise.resolve();
  equal(settled, false);
  releaseStats({ total: 1 });
  const cache = await complete;
  equal(cache.cachedAt, '2026-08-10T00:00:00Z');
  equal(cache.portfolio.current_month_spending, 9479);
  equal(cache.stats.total, 1);

  let rejected = false;
  await fetchCompleteDashboardCache({
    accounts: async () => [] as never,
    portfolio: async () => { throw new Error('portfolio unavailable'); },
    stats: async () => ({ total: 1 }) as never,
    now: () => 'must-not-be-used',
  }).catch(() => { rejected = true; });
  equal(rejected, true);
  equal(hasNewerDashboardRevision([2, 1, 1], [1, 1, 1]), true);
  equal(hasNewerDashboardRevision([1, 1, 1], [1, 1, 1]), false);

  console.log('dashboard cache coordination tests passed');
}

void main();
