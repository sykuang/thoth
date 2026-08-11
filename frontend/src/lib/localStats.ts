import type { CardDateBasis, DashboardStats, Transaction } from '@/types/api';

import { transactionDateForBasis } from './transactionTimeline';
import { txnCashflowAmount, txnCashflowDirection } from './txnFilter';

const PASSIVE_INCOME = new Set(['interest_dividend', 'investment_gain']);
const INCOME_CATEGORIES = ['salary', 'bonus', 'interest_dividend', 'investment_gain', 'other'];

function sortedDescending<T>(record: Record<string, T>): Record<string, T> {
  return Object.fromEntries(Object.entries(record).sort(([left], [right]) => right.localeCompare(left)));
}

function roundPercentageOneDecimal(numerator: number, denominator: number): number {
  if (denominator <= 0) return 0;
  const scaled = BigInt(numerator) * 1000n;
  const divisor = BigInt(denominator);
  let quotient = scaled / divisor;
  const doubledRemainder = (scaled % divisor) * 2n;
  if (doubledRemainder > divisor
    || (doubledRemainder === divisor && quotient % 2n !== 0n)) quotient += 1n;
  return Number(quotient) / 10;
}

export function computeLocalDashboardStats(
  transactions: Transaction[],
  cardDateBasis: CardDateBasis,
): DashboardStats {
  const amountByMonth: DashboardStats['amount_by_month'] = {};
  const amountByCategory: Record<string, number> = {};
  const byKind: Record<string, number> = {};
  const amountByFlowType: Record<string, number> = {
    expense: 0,
    income: 0,
    transfer: 0,
    investment: 0,
  };
  const subscriptionByMonth: Record<string, number> = {};
  const amountByIncomeCategory = Object.fromEntries(
    INCOME_CATEGORIES.map((category) => [category, 0]),
  );
  const passiveIncomeByMonth: Record<string, number> = {};
  let totalIncome = 0;
  let totalExpense = 0;
  let subscriptionTotal = 0;
  let passiveIncomeTotal = 0;
  let incomeUnclassifiedCount = 0;

  for (const transaction of transactions) {
    byKind[transaction.kind] = (byKind[transaction.kind] ?? 0) + 1;
    if (transaction.excluded || transaction.auto_excluded) continue;

    const month = transactionDateForBasis(transaction, cardDateBasis).slice(0, 7);
    const direction = txnCashflowDirection(transaction);
    const amount = Math.abs(txnCashflowAmount(transaction));
    const flowType = transaction.flow_type;
    if (flowType && flowType in amountByFlowType) {
      amountByFlowType[flowType] += amount;
    }
    if (transaction.is_subscription && direction === 'expense') {
      subscriptionTotal += amount;
      if (month) subscriptionByMonth[month] = (subscriptionByMonth[month] ?? 0) + amount;
    }
    if (flowType === 'income' && direction === 'income') {
      const incomeCategory = transaction.income_category;
      if (incomeCategory && incomeCategory in amountByIncomeCategory) {
        amountByIncomeCategory[incomeCategory] += amount;
        if (PASSIVE_INCOME.has(incomeCategory)) {
          passiveIncomeTotal += amount;
          if (month) passiveIncomeByMonth[month] = (passiveIncomeByMonth[month] ?? 0) + amount;
        }
      } else {
        incomeUnclassifiedCount += 1;
      }
    }
    if (month) {
      const bucket = amountByMonth[month] ?? { income: 0, expense: 0, net: 0, count: 0 };
      bucket.count += 1;
      if (direction === 'income') {
        bucket.income += amount;
        bucket.net += amount;
        totalIncome += amount;
      } else if (direction === 'expense') {
        bucket.expense += amount;
        bucket.net -= amount;
        totalExpense += amount;
      }
      amountByMonth[month] = bucket;
    }
    if (transaction.category && direction === 'expense') {
      amountByCategory[transaction.category] = (amountByCategory[transaction.category] ?? 0) + amount;
    }
  }

  return {
    total: transactions.length,
    total_income: totalIncome,
    total_expense: totalExpense,
    total_net: totalIncome - totalExpense,
    amount_by_month: sortedDescending(amountByMonth),
    amount_by_category: Object.fromEntries(
      Object.entries(amountByCategory).sort(([, left], [, right]) => right - left),
    ),
    by_kind: byKind,
    amount_by_flow_type: amountByFlowType,
    subscription_total: subscriptionTotal,
    subscription_by_month: sortedDescending(subscriptionByMonth),
    amount_by_income_category: amountByIncomeCategory,
    passive_income_total: passiveIncomeTotal,
    passive_income_by_month: sortedDescending(passiveIncomeByMonth),
    passive_income_pct: roundPercentageOneDecimal(passiveIncomeTotal, totalIncome),
    income_unclassified_count: incomeUnclassifiedCount,
  };
}
