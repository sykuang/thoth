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
