import type {
  BankAccount,
  BankAccountBalance,
  Card,
  DashboardStats,
  FinancialAccount,
  PortfolioSummary,
  Transaction,
  TransactionSplit,
  UserPreferences,
} from '@/types/api';

export const REPLICA_SCHEMA_VERSION = 1;

export type ReplicaPartition = {
  name: string;
  generation: number;
  data: unknown;
};

export type ReplicaResponse = {
  schema_version: number;
  owner_id: number;
  reset_required: boolean;
  generations: Record<string, number>;
  partitions: ReplicaPartition[];
};

export type ReplicaDashboardCache = {
  cachedAt: string;
  accounts: BankAccount[];
  portfolio: PortfolioSummary;
  stats: DashboardStats;
};

export type ReplicaAccountTabCache = {
  cachedAt: string;
  balances: BankAccountBalance[];
  accounts: BankAccount[];
  cards: Card[];
  manualAccounts: FinancialAccount[];
};

export type ReplicaEnvelope = {
  ownerKey: string;
  ownerId: number;
  schemaVersion: number;
  generations: Record<string, number>;
  partitions: Record<string, unknown>;
  syncedAt: string;
  dashboardCache?: ReplicaDashboardCache;
  accountTabCache?: ReplicaAccountTabCache;
};

export type ReplicaTransactionDataset = {
  cursor: string;
  transactions: Transaction[];
  preferences: UserPreferences;
  dashboardCache?: ReplicaDashboardCache;
  accountTabCache?: ReplicaAccountTabCache;
};

export type ReplicaStore = {
  load(ownerKey: string): Promise<ReplicaEnvelope | undefined>;
  save(envelope: ReplicaEnvelope): Promise<void>;
  clear(ownerKey: string): Promise<void>;
};

export type ReplicaRequest = (
  path: '/replica/bootstrap' | '/replica/pull',
  body?: { schema_version: number; generations: Record<string, number> },
) => Promise<ReplicaResponse>;

const ownerQueues = new Map<string, Promise<unknown>>();
const ownerEpochs = new Map<string, number>();
const blockedOwners = new Set<string>();
const pendingOwnerClears = new Map<string, Promise<void>>();
const activateAfterClear = new Set<string>();

export class ReplicaSyncCancelledError extends Error {
  constructor() {
    super('Replica sync cancelled by owner transition');
    this.name = 'ReplicaSyncCancelledError';
  }
}

function enqueueOwnerTask<T>(ownerKey: string, task: () => Promise<T>): Promise<T> {
  const previous = ownerQueues.get(ownerKey) ?? Promise.resolve();
  const current = previous.catch(() => undefined).then(task);
  ownerQueues.set(ownerKey, current);
  void current.finally(() => {
    if (ownerQueues.get(ownerKey) === current) ownerQueues.delete(ownerKey);
  }).catch(() => undefined);
  return current;
}

function assertOwnerActive(ownerKey: string, epoch: number): void {
  if (blockedOwners.has(ownerKey) || ownerEpochs.get(ownerKey) !== epoch) {
    throw new ReplicaSyncCancelledError();
  }
}

export function activateReplicaOwner(ownerKey: string): void {
  if (!ownerEpochs.has(ownerKey)) ownerEpochs.set(ownerKey, 0);
  if (pendingOwnerClears.has(ownerKey)) {
    activateAfterClear.add(ownerKey);
    return;
  }
  blockedOwners.delete(ownerKey);
}

export async function waitForReplicaOwner(ownerKey: string, epoch: number): Promise<void> {
  const pendingClear = pendingOwnerClears.get(ownerKey);
  if (pendingClear) await pendingClear;
  assertOwnerActive(ownerKey, epoch);
}

export function getReplicaOwnerEpoch(ownerKey: string): number {
  if (!ownerEpochs.has(ownerKey)) ownerEpochs.set(ownerKey, 0);
  return ownerEpochs.get(ownerKey) ?? 0;
}

export function assertReplicaOwnerEpoch(ownerKey: string, epoch: number): void {
  assertOwnerActive(ownerKey, epoch);
}

export async function guardReplicaOwnerRequest<T>(
  ownerKey: string,
  epoch: number,
  request: () => Promise<T>,
): Promise<T> {
  assertOwnerActive(ownerKey, epoch);
  const result = await request();
  assertOwnerActive(ownerKey, epoch);
  return result;
}

export async function updateReplicaPreferences(
  store: ReplicaStore,
  ownerKey: string,
  epoch: number,
  preferences: UserPreferences,
): Promise<void> {
  await enqueueOwnerTask(ownerKey, async () => {
    assertOwnerActive(ownerKey, epoch);
    const current = await store.load(ownerKey);
    assertOwnerActive(ownerKey, epoch);
    if (!current || current.schemaVersion !== REPLICA_SCHEMA_VERSION) return;
    const userPartition = current.partitions.user;
    const user = userPartition && typeof userPartition === 'object'
      ? userPartition as Record<string, unknown>
      : {};
    await store.save({
      ...current,
      partitions: {
        ...current.partitions,
        user: { ...user, preferences },
      },
    });
    assertOwnerActive(ownerKey, epoch);
  });
}

export async function updateReplicaDashboardCache(
  store: ReplicaStore,
  ownerKey: string,
  epoch: number,
  dashboardCache: ReplicaDashboardCache,
): Promise<void> {
  await enqueueOwnerTask(ownerKey, async () => {
    assertOwnerActive(ownerKey, epoch);
    const current = await store.load(ownerKey);
    assertOwnerActive(ownerKey, epoch);
    if (!current || current.schemaVersion !== REPLICA_SCHEMA_VERSION) return;
    await store.save({ ...current, dashboardCache });
    assertOwnerActive(ownerKey, epoch);
  });
}

export async function updateReplicaAccountTabCache(
  store: ReplicaStore,
  ownerKey: string,
  epoch: number,
  accountTabCache: ReplicaAccountTabCache,
): Promise<void> {
  await enqueueOwnerTask(ownerKey, async () => {
    assertOwnerActive(ownerKey, epoch);
    const current = await store.load(ownerKey);
    assertOwnerActive(ownerKey, epoch);
    if (!current || current.schemaVersion !== REPLICA_SCHEMA_VERSION) return;
    await store.save({ ...current, accountTabCache });
    assertOwnerActive(ownerKey, epoch);
  });
}

export async function patchReplicaAccountTabCache(
  store: ReplicaStore,
  ownerKey: string,
  epoch: number,
  updater: (cache: ReplicaAccountTabCache) => ReplicaAccountTabCache,
): Promise<void> {
  await enqueueOwnerTask(ownerKey, async () => {
    assertOwnerActive(ownerKey, epoch);
    const current = await store.load(ownerKey);
    assertOwnerActive(ownerKey, epoch);
    if (!current || current.schemaVersion !== REPLICA_SCHEMA_VERSION
      || !current.accountTabCache) return;
    await store.save({ ...current, accountTabCache: updater(current.accountTabCache) });
    assertOwnerActive(ownerKey, epoch);
  });
}

export async function syncReplica(
  store: ReplicaStore,
  ownerKey: string,
  request: ReplicaRequest,
): Promise<ReplicaEnvelope> {
  if (!ownerEpochs.has(ownerKey)) ownerEpochs.set(ownerKey, 0);
  const epoch = ownerEpochs.get(ownerKey) ?? 0;
  return enqueueOwnerTask(ownerKey, async () => {
    const requestForActiveOwner: ReplicaRequest = async (path, body) => {
      try {
        const response = await request(path, body);
        assertOwnerActive(ownerKey, epoch);
        return response;
      } catch (error) {
        assertOwnerActive(ownerKey, epoch);
        throw error;
      }
    };
    assertOwnerActive(ownerKey, epoch);
    const current = await store.load(ownerKey);
    assertOwnerActive(ownerKey, epoch);
    if (current) {
      const pulled = await requestForActiveOwner('/replica/pull', {
        schema_version: current.schemaVersion,
        generations: current.generations,
      });
      const merged = applyReplicaResponse(current, pulled, ownerKey);
      if (merged) {
        await store.save(merged);
        assertOwnerActive(ownerKey, epoch);
        return merged;
      }
      await store.clear(ownerKey);
      assertOwnerActive(ownerKey, epoch);
    }

    const response = await requestForActiveOwner('/replica/bootstrap');
    const bootstrapped = applyReplicaResponse(undefined, response, ownerKey);
    if (!bootstrapped) {
      throw new Error('Replica bootstrap returned an incompatible owner or schema');
    }
    await store.save(bootstrapped);
    assertOwnerActive(ownerKey, epoch);
    return bootstrapped;
  });
}

export function makeReplicaOwnerKey(serverUrl: string, email: string): string {
  const rawServer = serverUrl.trim();
  let server = rawServer.replace(/\/+$/, '');
  try {
    const parsed = new URL(rawServer);
    server = `${parsed.origin}${parsed.pathname}`.replace(/\/+$/, '');
  } catch {
    // Preserve non-standard development URLs byte-for-byte except trailing slash.
  }
  return JSON.stringify([server, email.trim()]);
}

export async function clearReplicaOwner(
  store: ReplicaStore,
  serverUrl: string,
  email: string | null,
): Promise<void> {
  if (!serverUrl || !email) return;
  const ownerKey = makeReplicaOwnerKey(serverUrl, email);
  blockedOwners.add(ownerKey);
  activateAfterClear.delete(ownerKey);
  ownerEpochs.set(ownerKey, (ownerEpochs.get(ownerKey) ?? 0) + 1);
  const clearTask = enqueueOwnerTask(ownerKey, async () => {
    await store.clear(ownerKey);
    const legacyKey = `${serverUrl.trim().replace(/\/+$/, '').toLowerCase()}|${email.trim().toLowerCase()}|v1`;
    if (legacyKey !== ownerKey) await store.clear(legacyKey);
  });
  pendingOwnerClears.set(ownerKey, clearTask);
  await clearTask;
  if (pendingOwnerClears.get(ownerKey) === clearTask) {
    pendingOwnerClears.delete(ownerKey);
    if (activateAfterClear.delete(ownerKey)) blockedOwners.delete(ownerKey);
  }
}

export function applyReplicaResponse(
  current: ReplicaEnvelope | undefined,
  response: ReplicaResponse,
  ownerKey: string,
): ReplicaEnvelope | undefined {
  if (
    response.reset_required
    || response.schema_version !== REPLICA_SCHEMA_VERSION
    || (current && (
      current.ownerKey !== ownerKey
      || current.ownerId !== response.owner_id
      || current.schemaVersion !== response.schema_version
    ))
  ) {
    return undefined;
  }

  const partitions = { ...(current?.partitions ?? {}) };
  const generations = { ...(current?.generations ?? {}) };
  for (const partition of response.partitions) {
    partitions[partition.name] = partition.data;
    generations[partition.name] = partition.generation;
  }
  for (const [name, generation] of Object.entries(response.generations)) {
    generations[name] = generation;
  }
  return {
    ownerKey,
    ownerId: response.owner_id,
    schemaVersion: response.schema_version,
    generations,
    partitions,
    syncedAt: new Date().toISOString(),
    dashboardCache: current?.dashboardCache,
    accountTabCache: current?.accountTabCache,
  };
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function validDashboardCache(value: unknown): value is ReplicaDashboardCache {
  const cache = asRecord(value);
  const portfolio = asRecord(cache?.portfolio);
  const stats = asRecord(cache?.stats);
  if (!cache || typeof cache.cachedAt !== 'string' || !Array.isArray(cache.accounts)) return false;
  if (!cache.accounts.every((row) => asRecord(row)?.has_creds === true
    || asRecord(row)?.has_creds === false)) return false;
  if (!portfolio || !Array.isArray(portfolio.by_bank)) return false;
  if (portfolio.as_of !== undefined && portfolio.as_of !== null
    && typeof portfolio.as_of !== 'string') return false;
  for (const key of [
    'total_assets',
    'fx_assets_twd',
    'brokerage_assets_twd',
    'total_liabilities',
    'total_card_unpaid',
    'total_loan',
    'current_month_spending',
    'net_worth_with_fx',
  ]) {
    if (typeof portfolio[key] !== 'number' || !Number.isFinite(portfolio[key])) return false;
  }
  const amountByMonth = asRecord(stats?.amount_by_month);
  if (!stats || !amountByMonth) return false;
  for (const key of ['total', 'total_income', 'total_expense', 'total_net']) {
    if (typeof stats[key] !== 'number' || !Number.isFinite(stats[key])) return false;
  }
  for (const key of [
    'subscription_total',
    'passive_income_total',
    'passive_income_pct',
    'income_unclassified_count',
  ]) {
    if (stats[key] !== undefined
      && (typeof stats[key] !== 'number' || !Number.isFinite(stats[key]))) return false;
  }
  for (const value of Object.values(amountByMonth)) {
    const bucket = asRecord(value);
    if (!bucket) return false;
    for (const key of ['income', 'expense', 'net', 'count']) {
      if (typeof bucket[key] !== 'number' || !Number.isFinite(bucket[key])) return false;
    }
  }
  for (const key of ['subscription_by_month', 'passive_income_by_month']) {
    if (stats[key] === undefined) continue;
    const amounts = asRecord(stats[key]);
    if (!amounts || Object.values(amounts).some(
      (amount) => typeof amount !== 'number' || !Number.isFinite(amount),
    )) return false;
  }
  return true;
}

function isNullableString(value: unknown): boolean {
  return value === null || typeof value === 'string';
}

function isNullableNumber(value: unknown): boolean {
  return value === null || (typeof value === 'number' && Number.isFinite(value));
}

function validBankAccount(value: unknown): boolean {
  const row = asRecord(value);
  return Boolean(row)
    && typeof row?.id === 'number'
    && Number.isInteger(row.id)
    && typeof row.bank === 'string'
    && typeof row.label === 'string'
    && typeof row.created_at === 'string'
    && typeof row.updated_at === 'string'
    && typeof row.has_creds === 'boolean'
    && Array.isArray(row.fields_set)
    && row.fields_set.every((field) => typeof field === 'string');
}

function validBankBalance(value: unknown): boolean {
  const row = asRecord(value);
  if (!row
    || typeof row.bank !== 'string'
    || typeof row.account_no !== 'string'
    || typeof row.currency !== 'string'
    || !isNullableString(row.nickname)
    || !isNullableString(row.product_type)
    || !isNullableString(row.type)
    || !isNullableNumber(row.balance)
    || !isNullableString(row.snapshot_date)
    || typeof row.is_stale !== 'boolean'
    || !isNullableNumber(row.twd_estimate)
    || !isNullableNumber(row.fx_rate_used)
    || typeof row.excluded !== 'boolean') return false;
  return row.nickname_overwrite === undefined || isNullableString(row.nickname_overwrite);
}

function validCard(value: unknown): boolean {
  const row = asRecord(value);
  if (!row
    || typeof row.card_no !== 'string'
    || typeof row.bank !== 'string'
    || !isNullableString(row.name)
    || !isNullableString(row.type)) return false;
  for (const key of [
    'nickname_overwrite',
    'association',
    'updated_at',
    'statement_close_date',
    'payment_due_date',
    'last_payment_date',
  ]) {
    if (row[key] !== undefined && !isNullableString(row[key])) return false;
  }
  for (const key of [
    'credit_limit',
    'used_credit',
    'available_credit',
    'bill_due_amount',
    'unbilled_amount',
    'last_payment_amount',
  ]) {
    if (row[key] !== undefined && !isNullableNumber(row[key])) return false;
  }
  if (row.excluded !== undefined && typeof row.excluded !== 'boolean') return false;
  if (row.is_cube !== undefined && row.is_cube !== null && typeof row.is_cube !== 'boolean') return false;
  return row.bill_status === undefined || [
    'due', 'paid', 'no_payment_required', 'overdue', 'unknown',
  ].includes(String(row.bill_status));
}

function validFinancialAccount(value: unknown): boolean {
  const row = asRecord(value);
  if (!row) return false;
  for (const key of ['id', 'source', 'source_ref', 'name', 'product_type', 'currency']) {
    if (typeof row[key] !== 'string') return false;
  }
  for (const key of ['institution_name', 'account_ref', 'balance', 'as_of']) {
    if (!isNullableString(row[key])) return false;
  }
  if (row.manual_balance !== undefined && !isNullableString(row.manual_balance)) return false;
  if (row.valuation_source !== null && ![
    'manual', 'yahoo_finance', 'manual_fallback',
  ].includes(String(row.valuation_source))) return false;
  return typeof row.included_in_net_worth === 'boolean'
    && typeof row.editable === 'boolean'
    && typeof row.deletable === 'boolean';
}

function validAccountTabCache(value: unknown): value is ReplicaAccountTabCache {
  const cache = asRecord(value);
  return Boolean(cache)
    && typeof cache?.cachedAt === 'string'
    && Array.isArray(cache.balances)
    && cache.balances.every(validBankBalance)
    && Array.isArray(cache.accounts)
    && cache.accounts.every(validBankAccount)
    && Array.isArray(cache.cards)
    && cache.cards.every(validCard)
    && Array.isArray(cache.manualAccounts)
    && cache.manualAccounts.every(validFinancialAccount);
}

function asRows(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((row): row is Record<string, unknown> => Boolean(row) && typeof row === 'object')
    : [];
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function maskTail(value: unknown): string | null {
  if (typeof value !== 'string' || !value) return null;
  if (value.length <= 4) return value;
  return `${'*'.repeat(value.length - 4)}${value.slice(-4)}`;
}

function normalizedSplits(row: Record<string, unknown>): TransactionSplit[] | undefined {
  if (!Array.isArray(row.splits) || row.splits.length < 2 || row.splits.length > 20) {
    return undefined;
  }
  const splits: TransactionSplit[] = [];
  let total = 0;
  for (const raw of row.splits) {
    if (!raw || typeof raw !== 'object') return undefined;
    const split = raw as Record<string, unknown>;
    if (!Number.isInteger(split.amount) || Number(split.amount) <= 0) return undefined;
    if (split.auto_excluded !== undefined && typeof split.auto_excluded !== 'boolean') {
      return undefined;
    }
    const amount = Number(split.amount);
    total += amount;
    splits.push({
      amount,
      category: stringOrNull(split.category),
      subcategory: stringOrNull(split.subcategory),
      note: stringOrNull(split.note),
      auto_excluded: Boolean(split.auto_excluded),
    });
  }
  const parentAmount = Math.abs(
    numberOrNull(row.cashflow_amount) ?? numberOrNull(row.amount) ?? 0,
  );
  return total === parentAmount ? splits : undefined;
}

function projectParent(row: Record<string, unknown>): Transaction {
  const amount = numberOrNull(row.amount) ?? 0;
  const cashflowAmount = Math.abs(numberOrNull(row.cashflow_amount) ?? amount);
  const kind = row.kind === 'billed' || row.kind === 'pending' ? row.kind : 'twd';
  const currency = stringOrNull(row.currency) ?? 'TWD';
  let consumeCurrency = stringOrNull(row.consume_currency);
  let consumeAmount = numberOrNull(row.consume_amount);
  let fxRate: number | null = null;
  let fxRateSource: Transaction['fx_rate_source'] = null;

  if (kind === 'billed' && amount !== 0 && consumeCurrency && consumeCurrency !== 'TWD' && consumeAmount) {
    fxRate = Math.abs(amount) / Math.abs(consumeAmount);
    fxRateSource = 'bank_billed';
  } else if (kind === 'pending' && currency !== 'TWD') {
    consumeCurrency = currency;
    consumeAmount = Math.abs(amount);
  } else if (kind === 'pending' && currency === 'TWD') {
    const match = stringOrNull(row.description)?.match(/\[([A-Z]{3})\s+(\d+(?:\.\d+)?)\]/);
    if (match && amount !== 0) {
      consumeCurrency = match[1];
      consumeAmount = Number(match[2]);
      if (consumeAmount > 0) {
        fxRate = Math.abs(amount) / consumeAmount;
        fxRateSource = 'bank_pending_estimate';
      }
    }
  }

  const tags: string[] = [];
  const seenTags = new Set<string>();
  if (Array.isArray(row.tags)) {
    for (const rawTag of row.tags) {
      if (typeof rawTag !== 'string') continue;
      const tag = rawTag.trim();
      if (!tag || seenTags.has(tag)) continue;
      seenTags.add(tag);
      tags.push(tag);
    }
  }
  const cardNo = stringOrNull(row.card_no);
  const accountNo = stringOrNull(row.account_no);
  return {
    id: typeof row.id === 'string' || typeof row.id === 'number' ? row.id : '',
    bank: stringOrNull(row.bank) ?? '',
    kind,
    date: stringOrNull(row.date),
    datetime: stringOrNull(row.datetime),
    description: stringOrNull(row.description),
    description_overwrite: stringOrNull(row.description_overwrite),
    amount,
    cashflow_direction: row.cashflow_direction === 'income'
      || row.cashflow_direction === 'expense'
      || row.cashflow_direction === 'neutral'
      ? row.cashflow_direction
      : undefined,
    cashflow_amount: cashflowAmount,
    display_amount: cashflowAmount,
    currency,
    category: stringOrNull(row.category),
    subcategory: stringOrNull(row.subcategory),
    legacy_category: stringOrNull(row.legacy_category),
    account_no: accountNo,
    card_no: cardNo,
    account_or_card: maskTail(accountNo ?? cardNo),
    balance: numberOrNull(row.balance),
    counterparty_bank: stringOrNull(row.counterparty_bank),
    counterparty_acct: stringOrNull(row.counterparty_acct),
    memo: stringOrNull(row.memo),
    display_description: stringOrNull(row.display_description) ?? stringOrNull(row.description),
    excluded: Boolean(row.excluded),
    auto_excluded: Boolean(row.auto_excluded),
    tags,
    splits: Array.isArray(row.splits) ? row.splits as TransactionSplit[] : [],
    consume_date: stringOrNull(row.consume_date),
    post_date: stringOrNull(row.post_date),
    consume_currency: consumeCurrency,
    consume_amount: consumeAmount,
    fx_rate: fxRate,
    fx_rate_source: fxRateSource,
    scope: stringOrNull(row.scope),
    txn_type: row.txn_type as Transaction['txn_type'],
    flow_type: row.flow_type as Transaction['flow_type'],
    is_subscription: Boolean(row.is_subscription),
    income_category: row.income_category as Transaction['income_category'],
  };
}

function projectTransaction(row: Record<string, unknown>): Transaction[] {
  const parent = projectParent(row);
  const splits = normalizedSplits(row);
  if (!splits) return [parent];
  return splits.map((split, index) => ({
    ...parent,
    id: `${parent.id}#${index}`,
    amount: parent.amount < 0 ? -split.amount : split.amount,
    cashflow_amount: split.amount,
    display_amount: split.amount,
    category: split.category,
    subcategory: split.subcategory,
    auto_excluded: Boolean(parent.auto_excluded) || Boolean(split.auto_excluded),
    split_of: typeof parent.id === 'number' ? parent.id : Number(parent.id),
    split_index: index,
    split_note: split.note,
    splits: [],
  }));
}

export function projectReplicaDataset(envelope: ReplicaEnvelope): ReplicaTransactionDataset {
  const groups: {
    rows: Transaction[];
    bankOrder: number;
    kindOrder: number;
    id: string | number;
    date: string;
    datetime: string;
  }[] = [];
  const kindOrder = new Map([['twd', 0], ['billed', 1], ['pending', 2]]);
  let bankOrder = 0;
  for (const [name, value] of Object.entries(envelope.partitions)) {
    if (!name.startsWith('bank:') || !value || typeof value !== 'object') continue;
    const bank = name.slice(5);
    const partition = value as Record<string, unknown>;
    for (const sourceRow of asRows(partition.transactions)) {
      const row = sourceRow.bank ? sourceRow : { ...sourceRow, bank };
      const projected = projectTransaction(row);
      const parent = projected[0];
      groups.push({
        rows: projected,
        bankOrder,
        kindOrder: kindOrder.get(parent.kind) ?? 99,
        id: parent.split_of ?? parent.id,
        date: parent.date ?? '',
        datetime: parent.datetime ?? '',
      });
    }
    bankOrder += 1;
  }
  groups.sort((left, right) => {
    const dateOrder = right.date.localeCompare(left.date);
    if (dateOrder !== 0) return dateOrder;
    const datetimeOrder = right.datetime.localeCompare(left.datetime);
    if (datetimeOrder !== 0) return datetimeOrder;
    if (left.bankOrder !== right.bankOrder) return left.bankOrder - right.bankOrder;
    if (left.kindOrder !== right.kindOrder) return left.kindOrder - right.kindOrder;
    const leftNumber = Number(left.id);
    const rightNumber = Number(right.id);
    if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) return rightNumber - leftNumber;
    return String(right.id).localeCompare(String(left.id), undefined, { numeric: true });
  });
  const transactions = groups.flatMap((group) => group.rows);
  const cursor = Object.entries(envelope.generations)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, generation]) => `${name}:${generation}`)
    .join('|');
  const userPartition = envelope.partitions.user;
  const rawPreferences = userPartition && typeof userPartition === 'object'
    ? (userPartition as Record<string, unknown>).preferences
    : undefined;
  const preferences = rawPreferences && typeof rawPreferences === 'object'
    ? rawPreferences as Record<string, unknown>
    : {};
  const fxDisplayMode = preferences.fx_display_mode;
  return {
    cursor,
    transactions,
    preferences: {
      fx_display_mode: fxDisplayMode === 'always_twd' || fxDisplayMode === 'always_original'
        ? fxDisplayMode
        : 'auto',
      card_date_basis: preferences.card_date_basis === 'post' ? 'post' : 'consume',
    },
    dashboardCache: validDashboardCache(envelope.dashboardCache)
      ? envelope.dashboardCache
      : undefined,
    accountTabCache: validAccountTabCache(envelope.accountTabCache)
      ? envelope.accountTabCache
      : undefined,
  };
}
