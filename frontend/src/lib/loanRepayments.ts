import type { LoanRepaymentFact, LoanTransaction, Transaction } from '@/types/api';
import { addDecimal } from './decimal';

export const LOAN_RECONCILIATION_WARNING = '貸款還款尚未核對：利息／違約金可能與存款扣款重複，請核對既有排除設定；不會自動排除扣款。台幣利息／違約金已納入收支統計；外幣依原幣另列，未換算為台幣。';
export function validLoanRepaymentFact(value: unknown): value is LoanRepaymentFact {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const row = value as Record<string, unknown>;
  return !('raw_json' in row) && ['id', 'bank', 'account_no', 'sub_account', 'currency'].every(k => typeof row[k] === 'string')
    && Boolean(row.id) && /^[A-Z]{3}$/.test(row.currency as string)
    && typeof row.source_account_id === 'number' && Number.isSafeInteger(row.source_account_id) && row.source_account_id > 0
    && ['due_date', 'paid_on', 'status', 'query_start', 'query_end'].every(k => row[k] === null || typeof row[k] === 'string')
    && ['principal', 'interest', 'penalty', 'paid_total', 'principal_balance'].every(k => typeof row[k] === 'string' && /^\d+(?:\.\d+)?$/.test(row[k] as string))
    && (row.excluded === undefined || typeof row.excluded === 'boolean');
}
export function projectLoanRepayment(fact: LoanRepaymentFact, excluded = false): LoanTransaction[] {
  if (!validLoanRepaymentFact(fact)) throw new Error('Invalid loan repayment fact');
  return (['principal', 'interest', 'penalty'] as const).flatMap(component => {
    const magnitude = addDecimal(fact[component], '0')!;
    if (magnitude === '0') return [];
    const principal = component === 'principal';
    const description = principal ? '貸款還本金' : component === 'interest' ? '貸款利息' : '貸款違約金';
    return [{id: `${fact.id}:${component}`, kind:'loan_repayment', component,
      bank:fact.bank, source_account_id:fact.source_account_id, account_no:fact.account_no, account_or_card:fact.account_no,
      currency:fact.currency, date:fact.paid_on, datetime:null, description, display_description:description,
      category:principal ? '還款' : '金融', subcategory:principal ? '本金' : component === 'interest' ? '貸款利息' : '違約金',
      amount:principal ? magnitude : `-${magnitude}`, display_amount:magnitude, display_sign:principal ? '+' : '-',
      cashflow_amount:principal ? '0' : magnitude, cashflow_direction:principal ? 'neutral' : 'expense',
      flow_type:principal ? 'transfer' : 'expense', read_only:true, reconciliation_status:'unverified',
      loan_repayment:fact, excluded:excluded || fact.excluded === true, auto_excluded:false }];
  });
}
export type LoanCurrencyStats = Record<string, { income:string; expense:string; net:string; count:number }>;
export function loanStatsByCurrency(rows: Transaction[]): LoanCurrencyStats {
  const result: LoanCurrencyStats = {};
  for (const row of rows) {
    if (row.kind !== 'loan_repayment' || row.excluded || row.auto_excluded) continue;
    const bucket = result[row.currency] ??= {income:'0', expense:'0', net:'0', count:0};
    if (row.component === 'principal') continue;
    bucket.expense = addDecimal(bucket.expense, row.cashflow_amount)!;
    bucket.net = `-${bucket.expense}`;
    bucket.count += 1;
  }
  return result;
}
