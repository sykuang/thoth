import {
  addDecimal,
  divideDecimal,
  formatDecimal,
  formatDecimalFixed,
  multiplyDecimal,
  multiplyDecimalToIntegerHalfEven,
} from './decimal';

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
assertEqual(multiplyDecimal('3', '33.33'), '99.99');
assertEqual(multiplyDecimal('2.5', '100.00'), '250');
assertEqual(multiplyDecimal('9007199254740993.01', '3'), '27021597764222979.03');
assertEqual(multiplyDecimal('0.0000000000005', '1'), '0.000000000001');
assertEqual(multiplyDecimal('0.0000000000004', '1'), '0');
assertEqual(multiplyDecimal('NaN', '1'), null);
assertEqual(addDecimal('0.1', '0.2'), '0.3');
assertEqual(addDecimal('-2.5', '1.25'), '-1.25');
assertEqual(addDecimal('9007199254740993.01', '0.99'), '9007199254740994');
assertEqual(divideDecimal('100', '3'), '33.33');
assertEqual(divideDecimal('10', '4'), '2.5');
assertEqual(divideDecimal('1', '8'), '0.13');
assertEqual(divideDecimal('9007199254740993', '3'), '3002399751580331');
assertEqual(divideDecimal('10', '0'), null);
assertEqual(formatDecimalFixed('NaN', 2), null);
if (multiplyDecimalToIntegerHalfEven('2.5', '1') !== 2) throw new Error('2.5 must round half-even to 2');
if (multiplyDecimalToIntegerHalfEven('3.5', '1') !== 4) throw new Error('3.5 must round half-even to 4');
if (multiplyDecimalToIntegerHalfEven('-3.5', '1') !== -4) throw new Error('-3.5 must round half-even to -4');
if (multiplyDecimalToIntegerHalfEven('+2.5', '1') !== 2) throw new Error('+2.5 must round half-even to 2');
if (multiplyDecimalToIntegerHalfEven('1000', '0.2') !== 200) throw new Error('FX conversion failed');
if (multiplyDecimalToIntegerHalfEven('NaN', '1') !== null) throw new Error('invalid decimal must fail');

console.log('decimal formatting checks passed');
