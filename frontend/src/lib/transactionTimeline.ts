import type { BrokerageAccount, BrokerageActivity, CardDateBasis, Transaction } from '@/types/api';

export type TransactionTimelineItem =
  | {
      source: 'bank';
      key: string;
      sortIndex: number;
      sortDay: string;
      sortDate: string;
      sortTimestamp: number | null;
      transaction: Transaction;
    }
  | {
      source: 'brokerage';
      key: string;
      sortIndex: number;
      sortDay: string;
      sortDate: string;
      sortTimestamp: number | null;
      activity: BrokerageActivity;
      account: BrokerageAccount | undefined;
    };

function timelineTimestamp(value: string): number | null {
  const text = value.trim();
  if (!text || /^\d{4}-\d{2}-\d{2}$/.test(text)) return null;
  let normalized = text.replace(' ', 'T').replace(/([+-]\d{2})(\d{2})$/, '$1:$2');
  if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(normalized)) normalized += '+08:00';
  const timestamp = Date.parse(normalized);
  return Number.isFinite(timestamp) ? timestamp : null;
}

export function transactionDateForBasis(
  transaction: Transaction,
  basis: CardDateBasis,
): string {
  if (transaction.kind === 'billed' || transaction.kind === 'pending') {
    if (basis === 'post') {
      return transaction.post_date ?? transaction.consume_date ?? transaction.date ?? '';
    }
    return transaction.consume_date ?? transaction.date ?? transaction.post_date ?? '';
  }
  return transaction.date ?? '';
}

export function mergeTransactionTimeline(
  bankRows: Transaction[],
  brokerageRows: BrokerageActivity[],
  accounts: BrokerageAccount[],
  cardDateBasis: CardDateBasis = 'consume',
): TransactionTimelineItem[] {
  const accountsById = new Map(accounts.map((account) => [account.id, account]));
  return [
    ...bankRows.map<TransactionTimelineItem>((transaction, sortIndex) => {
      const sortDay = transactionDateForBasis(transaction, cardDateBasis);
      const useTransactionTime = cardDateBasis === 'consume'
        || (transaction.kind !== 'billed' && transaction.kind !== 'pending');
      const sortDate = useTransactionTime ? (transaction.datetime ?? sortDay) : sortDay;
      return {
        source: 'bank',
        key: `bank:${transaction.bank}:${transaction.kind}:${transaction.id}`,
        sortIndex,
        sortDay: sortDay.slice(0, 10),
        sortDate,
        sortTimestamp: timelineTimestamp(sortDate),
        transaction,
      };
    }),
    ...brokerageRows.map<TransactionTimelineItem>((activity, activityIndex) => {
      const sortDate = activity.trade_date ?? activity.settlement_date ?? '';
      return {
        source: 'brokerage',
        key: `brokerage:${activity.account_id}:${activity.id}`,
        sortIndex: bankRows.length + activityIndex,
        sortDay: sortDate.slice(0, 10),
        sortDate,
        sortTimestamp: timelineTimestamp(sortDate),
        activity,
        account: accountsById.get(activity.account_id),
      };
    }),
  ].sort((a, b) => {
    const dayOrder = b.sortDay.localeCompare(a.sortDay);
    if (dayOrder !== 0) return dayOrder;
    if (a.sortTimestamp != null && b.sortTimestamp != null && a.sortTimestamp !== b.sortTimestamp) {
      return b.sortTimestamp - a.sortTimestamp;
    }
    return a.sortIndex - b.sortIndex;
  });
}