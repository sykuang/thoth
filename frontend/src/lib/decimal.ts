export function formatDecimal(value: string): string | null {
  const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match) return null;
  const [, sign, rawInteger, fraction] = match;
  const integer = rawInteger.replace(/^0+(?=\d)/, '');
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return `${sign}${grouped}${fraction == null ? '' : `.${fraction}`}`;
}

export function formatDecimalFixed(value: string, fractionDigits: number): string | null {
  if (!Number.isInteger(fractionDigits) || fractionDigits < 0) return null;
  const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match) return null;
  const [, sign, rawInteger, rawFraction = ''] = match;
  const fraction = rawFraction.padEnd(fractionDigits, '0').slice(0, fractionDigits);
  let digits = `${rawInteger}${fraction}`;
  if ((rawFraction[fractionDigits] ?? '0') >= '5') {
    const rounded = digits.split('');
    let carry = 1;
    for (let index = rounded.length - 1; index >= 0 && carry; index -= 1) {
      const next = Number(rounded[index]) + carry;
      rounded[index] = String(next % 10);
      carry = next >= 10 ? 1 : 0;
    }
    if (carry) rounded.unshift('1');
    digits = rounded.join('');
  }
  const padded = digits.padStart(fractionDigits + 1, '0');
  const integer = fractionDigits ? padded.slice(0, -fractionDigits) : padded;
  const fixedFraction = fractionDigits ? padded.slice(-fractionDigits) : '';
  return formatDecimal(`${sign}${integer}${fractionDigits ? `.${fixedFraction}` : ''}`);
}

type UnsignedDecimal = { digits: bigint; scale: number };

function parseUnsignedDecimal(value: string): UnsignedDecimal | null {
  const match = /^(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match) return null;
  const fraction = match[2] ?? '';
  return {
    digits: BigInt(`${match[1]}${fraction}`),
    scale: fraction.length,
  };
}

function powerOfTen(exponent: number): bigint {
  return 10n ** BigInt(exponent);
}

function roundedToScale(digits: bigint, scale: number, targetScale: number): UnsignedDecimal {
  if (scale <= targetScale) return { digits, scale };
  const divisor = powerOfTen(scale - targetScale);
  const quotient = digits / divisor;
  const remainder = digits % divisor;
  return {
    digits: quotient + (remainder * 2n >= divisor ? 1n : 0n),
    scale: targetScale,
  };
}

function decimalText({ digits, scale }: UnsignedDecimal): string {
  while (scale > 0 && digits % 10n === 0n) {
    digits /= 10n;
    scale -= 1;
  }
  if (scale === 0) return digits.toString();
  const padded = digits.toString().padStart(scale + 1, '0');
  return `${padded.slice(0, -scale)}.${padded.slice(-scale)}`;
}

function signedDecimalText(digits: bigint, scale: number): string {
  if (digits < 0n) return `-${decimalText({ digits: -digits, scale })}`;
  return decimalText({ digits, scale });
}

function parseSignedDecimal(value: string): { digits: bigint; scale: number } | null {
  const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match) return null;
  const fraction = match[3] ?? '';
  const digits = BigInt(`${match[2]}${fraction}`);
  return {
    digits: match[1] === '-' ? -digits : digits,
    scale: fraction.length,
  };
}

/** Exact fixed-point addition for signed decimal strings. */
export function addDecimal(value: string, addend: string): string | null {
  const left = parseSignedDecimal(value);
  const right = parseSignedDecimal(addend);
  if (!left || !right) return null;
  const scale = Math.max(left.scale, right.scale);
  const leftDigits = left.digits * powerOfTen(scale - left.scale);
  const rightDigits = right.digits * powerOfTen(scale - right.scale);
  return signedDecimalText(leftDigits + rightDigits, scale);
}

/** Exact fixed-point multiplication, rounded half-up only beyond maxFractionDigits. */
export function multiplyDecimal(
  value: string,
  multiplier: string,
  maxFractionDigits = 12,
): string | null {
  if (!Number.isInteger(maxFractionDigits) || maxFractionDigits < 0) return null;
  const left = parseUnsignedDecimal(value);
  const right = parseUnsignedDecimal(multiplier);
  if (!left || !right) return null;
  return decimalText(roundedToScale(
    left.digits * right.digits,
    left.scale + right.scale,
    maxFractionDigits,
  ));
}

/** Exact fixed-point multiplication without intermediate rounding. */
export function multiplyDecimalExact(value: string, multiplier: string): string | null {
  const left = parseUnsignedDecimal(value);
  const right = parseUnsignedDecimal(multiplier);
  if (!left || !right) return null;
  return decimalText({
    digits: left.digits * right.digits,
    scale: left.scale + right.scale,
  });
}

/** Exact fixed-point multiplication rounded to an integer with banker's rounding. */
export function multiplyDecimalToIntegerHalfEven(
  value: string,
  multiplier: string,
): number | null {
  const left = parseSignedDecimal(value);
  const right = parseSignedDecimal(multiplier);
  if (!left || !right) return null;
  const product = left.digits * right.digits;
  const negative = product < 0n;
  const digits = negative ? -product : product;
  const divisor = powerOfTen(left.scale + right.scale);
  let rounded = digits / divisor;
  const doubledRemainder = (digits % divisor) * 2n;
  if (doubledRemainder > divisor || (doubledRemainder === divisor && rounded % 2n !== 0n)) {
    rounded += 1n;
  }
  const result = Number(negative ? -rounded : rounded);
  return Number.isSafeInteger(result) ? result : null;
}

/** Fixed-point division for display, rounded half-up and stripped to at most N decimals. */
export function divideDecimal(
  value: string,
  divisor: string,
  maxFractionDigits = 2,
): string | null {
  if (!Number.isInteger(maxFractionDigits) || maxFractionDigits < 0) return null;
  const numeratorValue = parseUnsignedDecimal(value);
  const denominatorValue = parseUnsignedDecimal(divisor);
  if (!numeratorValue || !denominatorValue || denominatorValue.digits === 0n) return null;
  const numerator = numeratorValue.digits
    * powerOfTen(denominatorValue.scale + maxFractionDigits);
  const denominator = denominatorValue.digits * powerOfTen(numeratorValue.scale);
  const quotient = numerator / denominator;
  const remainder = numerator % denominator;
  return decimalText({
    digits: quotient + (remainder * 2n >= denominator ? 1n : 0n),
    scale: maxFractionDigits,
  });
}
