import type { CardDateBasis, DashboardStats, Transaction } from '@/types/api';

import { addMoney, absMoney, negateMoney, moneySign, type Money } from './money';
import { transactionDateForBasis } from './transactionTimeline';
import { loanStatsByCurrency } from './loanRepayments';
import { txnCashflowAmount, txnCashflowDirection } from './txnFilter';

const PASSIVE_INCOME = new Set(['interest_dividend', 'investment_gain']);
const INCOME_CATEGORIES = ['salary', 'bonus', 'interest_dividend', 'investment_gain', 'other'];

function sortedDescending<T>(record: Record<string, T>): Record<string, T> {
  return Object.fromEntries(Object.entries(record).sort(([left], [right]) => right.localeCompare(left)));
}

function roundPercentageOneDecimal(numerator: Money, denominator: Money): number {
  if (moneySign(denominator) <= 0) return 0;
  const [ni, nf = ''] = String(numerator).split('.');
  const [di, df = ''] = String(denominator).split('.');
  const scaled = BigInt(ni + nf) * 1000n * 10n ** BigInt(df.length);
  const divisor = BigInt(di + df) * 10n ** BigInt(nf.length);
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
  const amountByMonth: DashboardStats['amount_by_month'] = Object.create(null);
  const amountByCategory: Record<string, Money> = Object.create(null);
  const byKind: Record<string, number> = Object.create(null);
  const amountByFlowType: Record<string, Money> = {
    expense: 0,
    income: 0,
    transfer: 0,
    investment: 0,
  };
  const subscriptionByMonth: Record<string, Money> = Object.create(null);
  const amountByIncomeCategory: Record<string, Money> = Object.fromEntries(
    INCOME_CATEGORIES.map((category) => [category, 0]),
  );
  const passiveIncomeByMonth: Record<string, Money> = Object.create(null);
  let totalIncome: Money = 0;
  let totalExpense: Money = 0;
  let subscriptionTotal: Money = 0;
  let passiveIncomeTotal: Money = 0;
  let incomeUnclassifiedCount = 0;

  for (const transaction of transactions) {
    byKind[transaction.kind] = (byKind[transaction.kind] ?? 0) + 1;
    if (transaction.excluded || transaction.auto_excluded) continue;
    if (transaction.kind === 'loan_repayment') {
      if (transaction.currency !== 'TWD') continue;
      const month = transactionDateForBasis(transaction, cardDateBasis).slice(0, 7);
      if (transaction.component === 'principal') {
        if (month) (amountByMonth[month] ??= {income:0, expense:0, net:0, count:0}).count += 1;
        continue;
      }
      const amount = absMoney(txnCashflowAmount(transaction));
      amountByFlowType.expense = addMoney(amountByFlowType.expense, amount);
      if (month) {
        const bucket = amountByMonth[month] ??= {income:0, expense:0, net:0, count:0};
        bucket.count += 1;
        bucket.expense = addMoney(bucket.expense, amount);
        bucket.net = addMoney(bucket.net, negateMoney(amount));
        totalExpense = addMoney(totalExpense, amount);
      }
      if (transaction.category) amountByCategory[transaction.category] = addMoney(amountByCategory[transaction.category] ?? 0, amount);
      continue;
    }

    const month = transactionDateForBasis(transaction, cardDateBasis).slice(0, 7);
    const direction = txnCashflowDirection(transaction);
    const signed = txnCashflowAmount(transaction);
    if (typeof signed !== 'number') throw new Error('Unexpected non-loan decimal cashflow');
    const amount = Math.abs(signed);
    const flowType = transaction.flow_type;
    if (flowType && Object.hasOwn(amountByFlowType, flowType)) {
      amountByFlowType[flowType] = addMoney(amountByFlowType[flowType], amount);
    }
    if (transaction.is_subscription && direction === 'expense') {
      subscriptionTotal = addMoney(subscriptionTotal, amount);
      if (month) subscriptionByMonth[month] = addMoney(subscriptionByMonth[month] ?? 0, amount);
    }
    if (flowType === 'income' && direction === 'income') {
      const incomeCategory = transaction.income_category;
      if (incomeCategory && Object.hasOwn(amountByIncomeCategory, incomeCategory)) {
        amountByIncomeCategory[incomeCategory] = addMoney(amountByIncomeCategory[incomeCategory], amount);
        if (PASSIVE_INCOME.has(incomeCategory)) {
          passiveIncomeTotal = addMoney(passiveIncomeTotal, amount);
          if (month) passiveIncomeByMonth[month] = addMoney(passiveIncomeByMonth[month] ?? 0, amount);
        }
      } else {
        incomeUnclassifiedCount += 1;
      }
    }
    if (month) {
      const bucket = amountByMonth[month] ?? { income: 0, expense: 0, net: 0, count: 0 };
      bucket.count += 1;
      if (direction === 'income') {
        bucket.income = addMoney(bucket.income, amount);
        bucket.net = addMoney(bucket.net, amount);
        totalIncome = addMoney(totalIncome, amount);
      } else if (direction === 'expense') {
        bucket.expense = addMoney(bucket.expense, amount);
        bucket.net = addMoney(bucket.net, -amount);
        totalExpense = addMoney(totalExpense, amount);
      }
      amountByMonth[month] = bucket;
    }
    if (transaction.category && direction === 'expense') {
      amountByCategory[transaction.category] = addMoney(amountByCategory[transaction.category] ?? 0, amount);
    }
  }

  return {
    ...(transactions.some(t => t.kind === 'loan_repayment') ? {loan_by_currency:loanStatsByCurrency(transactions)} : {}),
    total: transactions.length,
    total_income: totalIncome,
    total_expense: totalExpense,
    total_net: addMoney(totalIncome, negateMoney(totalExpense)),
    amount_by_month: sortedDescending(amountByMonth),
    amount_by_category: Object.fromEntries(
      Object.entries(amountByCategory).sort(([, left], [, right]) => moneySign(addMoney(right, negateMoney(left)))),
    ),
    by_kind: { ...byKind },
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
