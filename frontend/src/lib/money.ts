import { addDecimal, divideDecimal, multiplyDecimalExact } from './decimal';
export type Money = number | string;
export function addMoney(a: Money, b: Money): Money {
  if (typeof a === 'number' && typeof b === 'number'
    && Number.isSafeInteger(a) && Number.isSafeInteger(b) && Number.isSafeInteger(a + b)) return a + b;
  const result = addDecimal(String(a), String(b));
  if (result === null) throw new Error('Invalid decimal money');
  return result;
}
export function negateMoney(a: Money): Money {
  return typeof a === 'number' ? -a : a.startsWith('-') ? a.slice(1) : `-${a}`;
}
export function absMoney(a: Money): Money {
  return typeof a === 'number' ? Math.abs(a) : a.replace(/^[+-]/, '');
}
export function moneySign(a: Money): number {
  const normalized = addDecimal(String(a), '0');
  if (normalized === null) throw new Error('Invalid decimal money');
  return normalized === '0' ? 0 : normalized.startsWith('-') ? -1 : 1;
}
/** Only the bounded display percentage becomes a Number, never money. */
export function moneyPercentage(a: Money, total: Money, fractionDigits = 4): number {
  if (moneySign(total) <= 0) return 0;
  return Number(divideDecimal(multiplyDecimalExact(String(absMoney(a)), '100')!, String(total), fractionDigits));
}
