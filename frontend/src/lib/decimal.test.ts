import { formatDecimal, formatDecimalFixed } from './decimal';

function assertEqual(actual: string | null, expected: string | null): void {
  if (actual !== expected) throw new Error(`expected ${expected}, got ${actual}`);
}

assertEqual(formatDecimal('0.0001'), '0.0001');
assertEqual(formatDecimal('-1000.5000'), '-1,000.5000');
assertEqual(formatDecimal('9007199254740993.01'), '9,007,199,254,740,993.01');
assertEqual(formatDecimal('NaN'), null);
assertEqual(formatDecimal('1e-8'), null);
assertEqual(formatDecimalFixed('1', 2), '1.00');
assertEqual(formatDecimalFixed('1.2', 2), '1.20');
assertEqual(formatDecimalFixed('1.235', 2), '1.24');
assertEqual(formatDecimalFixed('9007199254740993.015', 2), '9,007,199,254,740,993.02');
assertEqual(formatDecimalFixed('-1.995', 2), '-2.00');
assertEqual(formatDecimalFixed('NaN', 2), null);

console.log('decimal formatting checks passed');
