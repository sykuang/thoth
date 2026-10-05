import { SUPPORTED_BANKS, type AutoDebitSetting, type PaymentReminder } from '@/types/api';
import type { ReplicaEnvelope } from './replica';
import { multiplyDecimalToIntegerHalfEven } from './decimal';

type CardFact = {
  bank: string; card_no: string; name: string | null; active: boolean; excluded: boolean;
  bill_due_amount: number | null; payment_due_date: string | null;
};
type AccountFact = { bank: string; account_no: string; raw_balance: number | null };
export type PaymentReminderInputs = {
  cards: CardFact[];
  settings: Pick<AutoDebitSetting, 'card_bank' | 'account_bank' | 'account_no'>[];
  accounts: AccountFact[];
};
const record = (v: unknown): Record<string, unknown> | undefined =>
  v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : undefined;
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
  && Math.abs(v) <= Number.MAX_SAFE_INTEGER;

/** Only ephemeral, narrowly validated facts; never persist a date-derived reminder. */
export function extractPaymentReminderInputs(envelope: ReplicaEnvelope): PaymentReminderInputs | undefined {
  const user = record(envelope.partitions.user);
  if (!Array.isArray(user?.bank_accounts) || !Array.isArray(user?.auto_debit_settings)) return undefined;
  const banks = new Set<string>();
  for (const value of user.bank_accounts) {
    const row = record(value);
    if (!row || typeof row.bank !== 'string' || !row.bank) return undefined;
    banks.add(row.bank);
  }
  const inputs: PaymentReminderInputs = { cards: [], settings: [], accounts: [] };
  for (const value of user.auto_debit_settings) {
    const row = record(value);
    if (!row || typeof row.card_bank !== 'string' || !row.card_bank
      || typeof row.account_bank !== 'string' || !row.account_bank
      || typeof row.account_no !== 'string' || !row.account_no
      || inputs.settings.some(s => s.card_bank === row.card_bank)) return undefined;
    inputs.settings.push({ card_bank: row.card_bank, account_bank: row.account_bank, account_no: row.account_no });
  }
  // Metadata defines source scope; without metadata the backend scans known banks.
  for (const bank of banks.size ? [...banks].sort() : SUPPORTED_BANKS) {
    const partition = record(envelope.partitions[`bank:${bank}`]);
    if (!Array.isArray(partition?.cards)) return undefined;
    for (const value of partition.cards) {
      const row = record(value);
      if (!row || row.bank !== bank || typeof row.card_no !== 'string'
        || !(row.name === null || typeof row.name === 'string')
        || typeof row.active !== 'boolean' || typeof row.excluded !== 'boolean'
        || !(row.bill_due_amount === null || finite(row.bill_due_amount))
        || !(row.payment_due_date === null || typeof row.payment_due_date === 'string')) return undefined;
      inputs.cards.push({ bank, card_no: row.card_no, name: row.name, active: row.active,
        excluded: row.excluded, bill_due_amount: row.bill_due_amount, payment_due_date: row.payment_due_date });
    }
  }
  for (const bank of new Set(inputs.settings.filter(s => inputs.cards.some(c => c.bank === s.card_bank)).map(s => s.account_bank))) {
    const partition = record(envelope.partitions[`bank:${bank}`]);
    if (!Array.isArray(partition?.accounts)) return undefined;
    for (const value of partition.accounts) {
      const row = record(value);
      if (!row || row.bank !== bank || typeof row.account_no !== 'string'
        || !(row.raw_balance === null || finite(row.raw_balance))) return undefined;
      inputs.accounts.push({ bank, account_no: row.account_no, raw_balance: row.raw_balance });
    }
  }
  return inputs;
}

export function taipeiPaymentDay(now = new Date()): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Taipei', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(now);
}

function dateMillis(value: string): number | undefined {
  const day = value.slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return undefined;
  const time = Date.parse(`${day}T00:00:00Z`);
  return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === day ? time : undefined;
}

/** Mirrors build_payment_reminders, using the business day, not the device zone. */
export function computeLocalPaymentReminders(
  inputs: PaymentReminderInputs | undefined,
  today = taipeiPaymentDay(),
): PaymentReminder[] | undefined {
  const day = dateMillis(today);
  if (!inputs || day === undefined) return undefined;
  const reminders: PaymentReminder[] = [];
  const seen = new Set<string>();
  for (const card of inputs.cards) {
    const due = card.payment_due_date === null ? undefined : dateMillis(card.payment_due_date);
    if (card.payment_due_date !== null && due === undefined) return undefined;
    if (!card.active || card.excluded || !card.bill_due_amount || card.bill_due_amount <= 0 || due === undefined) continue;
    const days = (due - day) / 86_400_000;
    if (days < 0 || days > 3) continue;
    const shared = card.bank !== 'hsbc';
    const key = JSON.stringify([card.bank, due, card.bill_due_amount]);
    if (shared && seen.has(key)) continue;
    seen.add(key);
    const setting = inputs.settings.find(s => s.card_bank === card.bank);
    const account = setting && inputs.accounts.find(a => a.bank === setting.account_bank && a.account_no === setting.account_no);
    const balance = account?.raw_balance ?? 0;
    if (setting && balance >= card.bill_due_amount) continue;
    // Python round(float, 2): retain the binary subtraction before half-even rounding.
    const cents = setting ? multiplyDecimalToIntegerHalfEven((card.bill_due_amount - balance).toFixed(100), '100') : 0;
    if (cents === null) return undefined;
    reminders.push({ reason: setting ? 'insufficient' : 'no_account', card_bank: card.bank,
      card_no: shared ? '' : card.card_no, card_name: shared ? null : card.name,
      bill_due_amount: card.bill_due_amount, payment_due_date: card.payment_due_date!, days_until_due: days,
      account_bank: setting?.account_bank ?? null, account_no: setting?.account_no ?? null,
      account_balance: setting ? balance : null, shortfall: setting ? cents / 100 : null });
  }
  return reminders.sort((a, b) => a.days_until_due - b.days_until_due || b.bill_due_amount - a.bill_due_amount);
}
