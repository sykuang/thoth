import {
  SUPPORTED_BANKS,
  type PortfolioBankSummary,
  type PortfolioSummary,
  type Transaction,
} from '@/types/api';

import { addDecimal, multiplyDecimalExact, multiplyDecimalToIntegerHalfEven } from './decimal';
import type { ReplicaEnvelope } from './replica';

const ASSET_TYPES = new Set(['deposit', 'time_deposit', 'fx_deposit', 'checking']);
const LIABILITY_TYPES = new Set(['loan', 'mortgage', 'credit_line']);
const STALE_DAYS = 90;
const SUPPORTED_BANK_SET = new Set<string>(SUPPORTED_BANKS);

type Row = Record<string, unknown>;

function record(value: unknown): Row | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Row
    : undefined;
}

function rows(value: unknown): Row[] {
  return Array.isArray(value) ? value.map(record).filter((row): row is Row => Boolean(row)) : [];
}

function finite(value: unknown): number | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value)) return undefined;
  return value;
}

function decimal(value: unknown): string | undefined {
  if (typeof value !== 'string' && typeof value !== 'number') return undefined;
  const text = String(value).trim();
  return /^[+-]?\d+(?:\.\d+)?$/.test(text) ? text : undefined;
}

function date(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const normalized = value.trim().slice(0, 10).replaceAll('/', '-');
  return /^\d{4}-\d{2}-\d{2}$/.test(normalized) ? normalized : undefined;
}

function latest(...values: (string | undefined)[]): string | undefined {
  return values.filter((value): value is string => Boolean(value)).sort().at(-1);
}

function isStale(value: string | undefined, now: Date): boolean {
  if (!value) return true;
  const timestamp = Date.parse(`${value}T00:00:00Z`);
  return !Number.isFinite(timestamp) || now.getTime() - timestamp > STALE_DAYS * 86_400_000;
}

function rateMap(envelope: ReplicaEnvelope): Row {
  const market = record(envelope.partitions.market);
  return record(record(market?.fx)?.rates) ?? {};
}

function convertToTwd(amount: unknown, currency: unknown, rates: Row): number | undefined {
  const value = decimal(amount);
  const code = typeof currency === 'string' ? currency.trim().toUpperCase() : '';
  const rate = code === 'TWD' ? 1 : finite(rates[code]);
  if (!value || !rate || rate <= 0) return undefined;
  return multiplyDecimalToIntegerHalfEven(value, String(rate)) ?? undefined;
}

function normalized(value: unknown): string {
  return typeof value === 'string' ? value.trim().toLowerCase() : '';
}

function accountBalances(partition: Row, loanAmount: number | undefined): Map<string, number> {
  const transactionBalances = new Map(
    rows(record(partition.portfolio_facts)?.latest_account_transaction_balances)
      .flatMap((row) => {
        const accountNo = typeof row.account_no === 'string' ? row.account_no : undefined;
        const balance = finite(row.balance);
        return accountNo && balance !== undefined ? [[accountNo, balance] as const] : [];
      }),
  );
  const balances = new Map<string, number>();
  for (const account of rows(partition.accounts)) {
    const accountNo = typeof account.account_no === 'string' ? account.account_no : undefined;
    if (!accountNo) continue;
    const productType = normalized(account.product_type);
    const raw = finite(account.raw_balance);
    const fallback = transactionBalances.get(accountNo)
      ?? (LIABILITY_TYPES.has(productType) ? loanAmount : undefined);
    if (raw !== undefined || fallback !== undefined) {
      const balance = raw ?? fallback ?? 0;
      balances.set(accountNo, LIABILITY_TYPES.has(productType) ? -Math.abs(balance) : balance);
    }
  }
  return balances;
}

function isCardExpense(transaction: Transaction): boolean {
  const transactionType = transaction.txn_type?.toLowerCase() ?? '';
  if (['cashback', 'refund', 'fee_waiver', 'payment'].includes(transactionType)) return false;
  const flowType = transaction.flow_type?.toLowerCase() ?? '';
  return flowType ? flowType === 'expense' : transaction.cashflow_direction === 'expense';
}

function currentMonthSpending(transactions: Transaction[], month: string, bank?: string): number {
  let total = 0;
  for (const transaction of transactions) {
    if (bank && transaction.bank !== bank) continue;
    if (transaction.kind !== 'pending' && transaction.kind !== 'billed') continue;
    if (transaction.currency.toUpperCase() !== 'TWD') continue;
    if (transaction.excluded || transaction.auto_excluded) continue;
    if (!isCardExpense(transaction)) continue;
    const consumeMonth = date(transaction.consume_date)?.slice(0, 7);
    if (transaction.kind === 'pending') {
      if (consumeMonth && consumeMonth !== month) continue;
    } else if (consumeMonth !== month) {
      continue;
    }
    const amount = finite(transaction.cashflow_amount) ?? Math.abs(transaction.amount);
    total += Math.abs(amount);
  }
  return total;
}

function bankSummary(
  bank: string,
  partition: Row,
  transactions: Transaction[],
  rates: Row,
  month: string,
  now: Date,
): { summary?: PortfolioBankSummary; assets: number; fx: number; card: number; loan: number; asOf?: string } {
  const facts = record(partition.portfolio_facts) ?? {};
  const balanceFact = record(facts.latest_twd_balance);
  const loanFact = record(facts.loan_balance);
  const cardFact = record(facts.card_unpaid);
  const rawAssets = finite(balanceFact?.twd_balance);
  const rawLoan = finite(loanFact?.amount_twd);
  const rawCard = finite(cardFact?.amount_twd);
  const balances = accountBalances(partition, rawLoan);
  let excludedTwd = 0;
  let excludedLoan = 0;
  let fx = 0;

  for (const account of rows(partition.accounts)) {
    const accountNo = typeof account.account_no === 'string' ? account.account_no : '';
    const productType = normalized(account.product_type);
    const currency = typeof account.currency === 'string' ? account.currency.trim().toUpperCase() : 'TWD';
    const balance = balances.get(accountNo);
    const excluded = account.excluded === true;
    if (LIABILITY_TYPES.has(productType)) {
      if (excluded && balance !== undefined) {
        excludedLoan += convertToTwd(Math.abs(balance), currency, rates) ?? 0;
      }
      continue;
    }
    if (currency === 'TWD') {
      if (excluded && balance !== undefined) {
        excludedTwd += convertToTwd(balance, currency, rates) ?? 0;
      }
      continue;
    }
    if (!excluded && balance !== undefined) fx += convertToTwd(balance, currency, rates) ?? 0;
  }

  const assets = Math.max((rawAssets ?? 0) - excludedTwd, 0);
  const loan = Math.max((rawLoan ?? 0) - excludedLoan, 0);
  const card = rawCard ?? 0;
  const spending = currentMonthSpending(transactions, month, bank);
  const cardAsOf = cardFact?.recognized === true ? date(cardFact.snapshot_date) : undefined;
  const asOf = latest(date(balanceFact?.snapshot_date), date(loanFact?.snapshot_date), cardAsOf);
  const hasData = rawAssets !== undefined || rawLoan !== undefined || rawCard !== undefined
    || spending !== 0 || fx !== 0;
  return {
    summary: hasData ? {
      bank,
      assets: rawAssets ?? null,
      fx_assets_twd: fx || null,
      liabilities: rawLoan !== undefined || rawCard !== undefined ? card + loan : null,
      card_unpaid: rawCard ?? null,
      loan_balance: rawLoan ?? null,
      current_month_spending: spending,
      stale: isStale(asOf, now),
      as_of: asOf ?? null,
    } : undefined,
    assets,
    fx,
    card,
    loan,
    asOf,
  };
}

function manualInvestmentValue(
  account: Row,
  transactions: Row[],
  quotes: Map<string, Row>,
  rates: Row,
): { value: string; asOf?: string } | undefined {
  const accountId = typeof account.id === 'string' ? account.id : '';
  const accountCurrency = typeof account.currency === 'string' ? account.currency.toUpperCase() : '';
  const fallback = decimal(account.balance);
  const totals = new Map<string, { symbol: string; currency: string; quantity: string }>();
  let invalid = false;

  const orderedTransactions = [...transactions].sort((left, right) => {
    const leftKey = `${String(left.occurred_on ?? '')}\u0000${String(left.id ?? '')}`;
    const rightKey = `${String(right.occurred_on ?? '')}\u0000${String(right.id ?? '')}`;
    return leftKey.localeCompare(rightKey, undefined, { numeric: true });
  });
  for (const transaction of orderedTransactions) {
    if (transaction.account_id !== accountId) continue;
    const kind = transaction.kind;
    if (kind === 'fee') continue;
    if (kind !== 'opening' && kind !== 'buy' && kind !== 'sell') continue;
    const symbol = typeof transaction.symbol === 'string' ? transaction.symbol.toUpperCase() : '';
    const currency = typeof transaction.currency === 'string' ? transaction.currency.toUpperCase() : '';
    const quantity = decimal(transaction.quantity);
    if (!symbol || !currency || !quantity || quantity.startsWith('-')) {
      invalid = true;
      break;
    }
    const key = JSON.stringify([symbol, currency]);
    const previous = totals.get(key)?.quantity ?? '0';
    const next = addDecimal(previous, kind === 'sell' ? `-${quantity}` : quantity);
    if (!next || next.startsWith('-')) {
      invalid = true;
      break;
    }
    totals.set(key, { symbol, currency, quantity: next });
  }

  if (invalid || totals.size === 0) return fallback ? { value: fallback } : undefined;
  let marketValue = '0';
  const quoteDates: string[] = [];
  for (const holding of totals.values()) {
    if (holding.quantity === '0') continue;
    const quote = quotes.get(holding.symbol);
    const price = decimal(quote?.regular_market_price);
    const quoteCurrency = typeof quote?.currency === 'string' ? quote.currency.toUpperCase() : '';
    if (!price || quoteCurrency !== holding.currency) return fallback ? { value: fallback } : undefined;
    const holdingValue = multiplyDecimalExact(holding.quantity, price);
    if (!holdingValue) return fallback ? { value: fallback } : undefined;
    if (holding.currency === accountCurrency) {
      marketValue = addDecimal(marketValue, holdingValue) ?? '';
    } else if (accountCurrency === 'TWD') {
      const converted = convertToTwd(holdingValue, holding.currency, rates);
      marketValue = converted === undefined ? '' : addDecimal(marketValue, String(converted)) ?? '';
    } else {
      marketValue = '';
    }
    if (!marketValue) return fallback ? { value: fallback } : undefined;
    const timestamp = finite(quote?.regular_market_time);
    if (timestamp !== undefined) quoteDates.push(new Date(timestamp * 1000).toISOString().slice(0, 10));
  }
  return { value: marketValue, asOf: quoteDates.sort()[0] };
}

function manualTotals(envelope: ReplicaEnvelope, rates: Row): {
  assets: number; liabilities: number; skipped: string[]; asOf?: string;
} {
  const partition = record(envelope.partitions.manual) ?? {};
  const transactions = rows(partition.transactions);
  const market = record(envelope.partitions.market) ?? {};
  const quotes = new Map(
    rows(market.quotes).flatMap((quote) => (
      typeof quote.symbol === 'string' ? [[quote.symbol.toUpperCase(), quote] as const] : []
    )),
  );
  let assets = 0;
  let liabilities = 0;
  const skipped: string[] = [];
  let asOf: string | undefined;

  for (const account of rows(partition.accounts)) {
    if (account.included_in_net_worth !== true) continue;
    const id = typeof account.id === 'string' ? account.id : '';
    const productType = normalized(account.product_type);
    const currency = typeof account.currency === 'string' ? account.currency : '';
    const valuation = productType === 'investment'
      ? manualInvestmentValue(account, transactions, quotes, rates)
      : (decimal(account.balance) ? { value: decimal(account.balance) as string, asOf: date(account.as_of) } : undefined);
    if (!valuation) continue;
    const converted = convertToTwd(valuation.value, currency, rates);
    if (converted === undefined) {
      if (id) skipped.push(id);
      continue;
    }
    if (LIABILITY_TYPES.has(productType)) liabilities += Math.abs(converted);
    else if (productType === 'investment' || ASSET_TYPES.has(productType)) assets += Math.max(converted, 0);
    asOf = latest(asOf, valuation.asOf);
  }
  return { assets, liabilities, skipped, asOf };
}

function brokerageTotals(envelope: ReplicaEnvelope, rates: Row): { assets: number; asOf?: string } {
  const partition = record(envelope.partitions.brokerage) ?? {};
  let assets = 0;
  for (const account of rows(partition.accounts)) {
    const converted = convertToTwd(account.balance_total, account.balance_currency, rates);
    if (converted !== undefined) assets += converted;
  }
  return { assets, asOf: date(partition.last_synced_at) };
}

export function computeLocalPortfolio(
  envelope: ReplicaEnvelope,
  transactions: Transaction[],
  now = new Date(),
): PortfolioSummary {
  const rates = rateMap(envelope);
  const month = now.toISOString().slice(0, 7);
  const byBank: PortfolioBankSummary[] = [];
  const skipped: string[] = [];
  let totalAssets = 0;
  let fxAssets = 0;
  let totalCard = 0;
  let totalLoan = 0;
  let overallAsOf: string | undefined;

  for (const [name, value] of Object.entries(envelope.partitions)) {
    if (!name.startsWith('bank:') || !SUPPORTED_BANK_SET.has(name.slice(5))) continue;
    const partition = record(value);
    if (!partition) continue;
    const result = bankSummary(name.slice(5), partition, transactions, rates, month, now);
    if (result.summary) {
      byBank.push(result.summary);
      overallAsOf = latest(overallAsOf, result.asOf);
    } else {
      skipped.push(name.slice(5));
    }
    totalAssets += result.assets;
    fxAssets += result.fx;
    totalCard += result.card;
    totalLoan += result.loan;
  }

  const manual = manualTotals(envelope, rates);
  const brokerage = brokerageTotals(envelope, rates);
  totalLoan += manual.liabilities;
  overallAsOf = latest(overallAsOf, manual.asOf, brokerage.asOf);
  byBank.sort((left, right) => (
    ((right.assets ?? 0) + (right.fx_assets_twd ?? 0))
    - ((left.assets ?? 0) + (left.fx_assets_twd ?? 0))
  ));
  const totalAssetsWithFx = totalAssets + fxAssets + brokerage.assets + manual.assets;
  const totalLiabilities = totalCard + totalLoan;
  return {
    total_assets: totalAssets,
    fx_assets_twd: fxAssets,
    brokerage_assets_twd: brokerage.assets,
    manual_assets_twd: manual.assets,
    manual_liabilities_twd: manual.liabilities,
    total_assets_with_fx: totalAssetsWithFx,
    total_liabilities: totalLiabilities,
    total_card_unpaid: totalCard,
    total_loan: totalLoan,
    current_month_spending: currentMonthSpending(transactions, month),
    net_worth: totalAssets - totalLiabilities,
    net_worth_with_fx: totalAssetsWithFx - totalLiabilities,
    as_of: overallAsOf ?? null,
    by_bank: byBank,
    skipped: [...skipped, ...manual.skipped],
  };
}
