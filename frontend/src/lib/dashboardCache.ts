import {
  SUPPORTED_BANKS,
  type BankAccount,
  type CardDateBasis,
  type Transaction,
} from '@/types/api';

import { computeLocalPortfolio } from './localPortfolio';
import { computeLocalDashboardStats } from './localStats';
import { addDecimal } from './decimal';
import type { ReplicaDashboardCache, ReplicaEnvelope } from './replica';

type Row = Record<string, unknown>;
const MANUAL_ASSET_TYPES = new Set(['deposit', 'time_deposit', 'fx_deposit', 'checking', 'investment']);
const MANUAL_LIABILITY_TYPES = new Set(['loan', 'mortgage', 'credit_line']);

function record(value: unknown): Row | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Row
    : undefined;
}

function nullableString(value: unknown): boolean {
  return value === null || typeof value === 'string';
}

function nullableFinite(value: unknown): boolean {
  return value === null || (
    typeof value === 'number'
    && Number.isFinite(value)
    && Math.abs(value) <= Number.MAX_SAFE_INTEGER
  );
}

function safeInteger(value: unknown): boolean {
  return typeof value === 'number' && Number.isSafeInteger(value);
}

function validDate(value: unknown, nullable = true): boolean {
  if (value === null) return nullable;
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const timestamp = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString().slice(0, 10) === value;
}

function safeProjection(value: unknown): boolean {
  if (typeof value === 'number') {
    return Number.isFinite(value) && Math.abs(value) <= Number.MAX_SAFE_INTEGER;
  }
  if (Array.isArray(value)) return value.every(safeProjection);
  const object = record(value);
  return !object || Object.values(object).every(safeProjection);
}

function decimal(value: unknown): boolean {
  const text = typeof value === 'string' || typeof value === 'number'
    ? String(value).trim()
    : '';
  return text.length <= 64 && /^[+-]?\d+(?:\.\d+)?$/.test(text);
}

function boundedDecimal(value: unknown, positive = false): boolean {
  const text = typeof value === 'string' || typeof value === 'number'
    ? String(value).trim()
    : '';
  const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(text);
  if (!match || text.length > 64) return false;
  const [, sign, whole, fraction = ''] = match;
  if (whole.replace(/^0+/, '').length > 15 || fraction.length > 12) return false;
  if (!positive) return true;
  return sign !== '-' && !/^0+(?:\.0+)?$/.test(text.replace(/^\+/, ''));
}

function negativeDecimal(value: unknown): boolean {
  const text = String(value).trim();
  return text.startsWith('-') && !/^-0(?:\.0+)?$/.test(text);
}

function validRows(value: unknown, validate: (row: Row) => boolean): boolean {
  return Array.isArray(value) && value.every((item) => {
    const row = record(item);
    return Boolean(row) && validate(row as Row);
  });
}

function validBankAccount(row: Row): boolean {
  return typeof row.account_no === 'string'
    && typeof row.currency === 'string'
    && nullableString(row.product_type)
    && nullableFinite(row.raw_balance)
    && validDate(row.raw_balance_date)
    && typeof row.excluded === 'boolean';
}

function validTransactionFact(row: Row): boolean {
  return (typeof row.id === 'number' || typeof row.id === 'string')
    && typeof row.bank === 'string'
    && ['twd', 'billed', 'pending'].includes(String(row.kind))
    && validDate(row.date)
    && validDate(row.consume_date)
    && validDate(row.post_date)
    && safeInteger(row.amount)
    && ['income', 'expense', 'neutral'].includes(String(row.cashflow_direction))
    && safeInteger(row.cashflow_amount)
    && typeof row.currency === 'string'
    && nullableString(row.consume_currency)
    && nullableFinite(row.consume_amount)
    && nullableString(row.category)
    && nullableString(row.subcategory)
    && nullableString(row.txn_type)
    && nullableString(row.flow_type)
    && typeof row.is_subscription === 'boolean'
    && nullableString(row.income_category)
    && typeof row.excluded === 'boolean'
    && typeof row.auto_excluded === 'boolean'
    && Array.isArray(row.splits);
}

function validBalanceFact(value: unknown): boolean {
  if (value === null) return true;
  const fact = record(value);
  if (!fact) return false;
  return validDate(fact.snapshot_date, false)
    && nullableFinite(fact.twd_balance);
}

function validLoanFact(value: unknown): boolean {
  if (value === null) return true;
  const fact = record(value);
  if (!fact) return false;
  return validDate(fact.snapshot_date)
    && nullableFinite(fact.amount_twd);
}

function validCardFact(value: unknown): boolean {
  if (value === null) return true;
  const fact = record(value);
  if (!fact) return false;
  return validDate(fact.snapshot_date, false)
    && nullableFinite(fact.amount_twd)
    && typeof fact.recognized === 'boolean';
}

function validPortfolioFacts(value: unknown): boolean {
  const facts = record(value);
  if (!facts) return false;
  for (const key of [
    'latest_twd_balance', 'latest_account_transaction_balances', 'loan_balance', 'card_unpaid',
  ]) {
    if (!(key in facts)) return false;
  }
  return validBalanceFact(facts.latest_twd_balance)
    && validRows(facts.latest_account_transaction_balances, (row) => (
      typeof row.account_no === 'string'
      && typeof row.balance === 'number' && Number.isFinite(row.balance)
    ))
    && validLoanFact(facts.loan_balance)
    && validCardFact(facts.card_unpaid);
}

function validManualAccount(row: Row): boolean {
  const productType = typeof row.product_type === 'string'
    ? row.product_type.trim().toLowerCase()
    : '';
  const validType = MANUAL_ASSET_TYPES.has(productType) || MANUAL_LIABILITY_TYPES.has(productType);
  const validBalance = row.balance === null || (
    boundedDecimal(row.balance)
    && (MANUAL_LIABILITY_TYPES.has(productType)
      ? negativeDecimal(row.balance) || /^[-+]?0(?:\.0+)?$/.test(String(row.balance).trim())
      : !negativeDecimal(row.balance))
  );
  return typeof row.id === 'string'
    && validType
    && typeof row.currency === 'string'
    && validBalance
    && typeof row.included_in_net_worth === 'boolean';
}

function validManualTransaction(row: Row): boolean {
  const kind = String(row.kind);
  const position = kind === 'opening' || kind === 'buy' || kind === 'sell';
  const amountValid = boundedDecimal(row.amount)
    && !negativeDecimal(row.amount)
    && (kind !== 'opening' || boundedDecimal(row.amount, true));
  return typeof row.id === 'number'
    && Number.isSafeInteger(row.id)
    && typeof row.account_id === 'string'
    && (position || kind === 'fee')
    && validDate(row.occurred_on, false)
    && /^[A-Z]{3}$/.test(String(row.currency))
    && amountValid
    && (position
      ? typeof row.symbol === 'string'
        && Boolean(row.symbol)
        && row.symbol === row.symbol.trim().toUpperCase()
        && boundedDecimal(row.quantity, true)
      : row.symbol === null && row.quantity === null && boundedDecimal(row.amount, true));
}

function validManualLedger(accounts: unknown, transactions: unknown): boolean {
  if (!Array.isArray(accounts) || !Array.isArray(transactions)) return false;
  const accountIds = accounts.flatMap((value) => {
    const account = record(value);
    return typeof account?.id === 'string' ? [account.id] : [];
  });
  if (accountIds.length !== accounts.length || new Set(accountIds).size !== accountIds.length) {
    return false;
  }
  const investmentIds = new Set(
    accounts.flatMap((value) => {
      const account = record(value);
      return account?.product_type === 'investment' && typeof account.id === 'string'
        ? [account.id]
        : [];
    }),
  );
  const entries = transactions.map(record);
  if (entries.some((entry) => !entry)) return false;
  entries.sort((left, right) => {
    const dateOrder = String(left?.occurred_on).localeCompare(String(right?.occurred_on));
    return dateOrder || Number(left?.id) - Number(right?.id);
  });
  const totals = new Map<string, string>();
  const transactionIds = new Set<number>();
  for (const entry of entries as Row[]) {
    const transactionId = Number(entry.id);
    if (transactionIds.has(transactionId)) return false;
    transactionIds.add(transactionId);
    if (!investmentIds.has(String(entry.account_id))) return false;
    if (entry.kind === 'fee') continue;
    const key = JSON.stringify([
      entry.account_id,
      String(entry.symbol).trim().toUpperCase(),
      entry.currency,
    ]);
    const quantity = String(entry.quantity);
    const next = addDecimal(
      totals.get(key) ?? '0',
      entry.kind === 'sell' ? `-${quantity}` : quantity,
    );
    if (!next || negativeDecimal(next)) return false;
    totals.set(key, next);
  }
  return true;
}

function validBrokerageAccount(row: Row): boolean {
  return (row.balance_total === null || decimal(row.balance_total))
    && nullableString(row.balance_currency);
}

function validQuote(row: Row): boolean {
  const timestamp = row.regular_market_time;
  return typeof row.symbol === 'string'
    && typeof row.currency === 'string'
    && boundedDecimal(row.regular_market_price, true)
    && (timestamp === null || (
      typeof timestamp === 'number'
      && Number.isFinite(timestamp)
      && Number.isFinite(new Date(timestamp * 1000).getTime())
    ));
}

function validUserAccount(row: Row): boolean {
  return typeof row.id === 'number'
    && typeof row.bank === 'string'
    && typeof row.label === 'string'
    && typeof row.created_at === 'string'
    && typeof row.updated_at === 'string'
    && typeof row.has_creds === 'boolean'
    && Array.isArray(row.fields_set)
    && row.fields_set.every((field) => typeof field === 'string');
}

export function projectReplicaDashboard(
  envelope: ReplicaEnvelope,
  transactions: Transaction[],
  cardDateBasis: CardDateBasis,
  now = new Date(),
): ReplicaDashboardCache | undefined {
  const user = record(envelope.partitions.user);
  if (!user || !validRows(user.bank_accounts, validUserAccount)) return undefined;

  const manual = record(envelope.partitions.manual);
  if (!manual
    || !validRows(manual.accounts, validManualAccount)
    || !validRows(manual.transactions, validManualTransaction)
    || !validManualLedger(manual.accounts, manual.transactions)) return undefined;

  const brokerage = record(envelope.partitions.brokerage);
  if (!brokerage
    || !validRows(brokerage.accounts, validBrokerageAccount)
    || !Array.isArray(brokerage.balances)
    || !Array.isArray(brokerage.positions)
    || !Array.isArray(brokerage.activities)) return undefined;

  const market = record(envelope.partitions.market);
  const fx = record(market?.fx);
  const rates = record(fx?.rates);
  if (!market || !fx || !rates || rates.TWD !== 1
    || Object.values(rates).some((rate) => (
      typeof rate !== 'number'
      || !Number.isFinite(rate)
      || rate <= 0
      || rate > Number.MAX_SAFE_INTEGER
    ))
    || !validRows(market.quotes, validQuote)) return undefined;

  for (const bank of SUPPORTED_BANKS) {
    const partition = record(envelope.partitions[`bank:${bank}`]);
    if (!partition
      || !validRows(partition.accounts, validBankAccount)
      || !Array.isArray(partition.cards)
      || !validRows(partition.transactions, validTransactionFact)
      || !validPortfolioFacts(partition.portfolio_facts)) return undefined;
  }
  try {
    const dashboard = {
      cachedAt: envelope.syncedAt,
      accounts: user.bank_accounts as BankAccount[],
      portfolio: computeLocalPortfolio(envelope, transactions, now),
      stats: computeLocalDashboardStats(transactions, cardDateBasis),
    };
    return safeProjection(dashboard) ? dashboard : undefined;
  } catch {
    return undefined;
  }
}
