import {
  SessionPromiseGate,
  storedCredentialsMatchSession,
} from './authRetryPolicy';

function equal(actual: unknown, expected: unknown, message?: string): void {
  if (actual !== expected) throw new Error(message ?? `Expected ${String(expected)}, got ${String(actual)}`);
}

function notEqual(actual: unknown, expected: unknown, message?: string): void {
  if (actual === expected) throw new Error(message ?? 'Expected values to differ');
}

async function main() {
  const gate = new SessionPromiseGate<number>();
  let starts = 0;
  let resolveA!: (value: number) => void;
  let resolveB!: (value: number) => void;
  const deferredA = new Promise<number>((resolve) => { resolveA = resolve; });
  const deferredB = new Promise<number>((resolve) => { resolveB = resolve; });

  const a1 = gate.getOrStart('owner-a:1', () => { starts += 1; return deferredA; });
  const a2 = gate.getOrStart('owner-a:1', () => { starts += 1; return Promise.resolve(99); });
  equal(a1, a2, 'same owner epoch must share one rotation');
  equal(starts, 1);

  const b1 = gate.getOrStart('owner-b:3', () => { starts += 1; return deferredB; });
  notEqual(a1, b1, 'different owner epoch must never join an old rotation');
  equal(starts, 2);
  const a3 = gate.getOrStart('owner-a:1', () => { starts += 1; return Promise.resolve(101); });
  equal(a1, a3, 'interleaved owners must retain one flight per owner epoch');
  equal(starts, 2);

  resolveA(1);
  equal(await a1, 1);
  const b2 = gate.getOrStart('owner-b:3', () => { starts += 1; return Promise.resolve(100); });
  equal(b1, b2, 'old owner completion must not clear the new owner flight');
  resolveB(2);
  equal(await b1, 2);

  equal(storedCredentialsMatchSession(
    { serverUrl: 'https://money.example/', email: 'USER@EXAMPLE.COM' },
    { serverUrl: 'https://money.example', email: 'user@example.com' },
  ), true);
  equal(storedCredentialsMatchSession(
    { serverUrl: 'https://old.example', email: 'user@example.com' },
    { serverUrl: 'https://money.example', email: 'user@example.com' },
  ), false);
  equal(storedCredentialsMatchSession(
    { serverUrl: 'https://money.example', email: 'old@example.com' },
    { serverUrl: 'https://money.example', email: 'user@example.com' },
  ), false);

  console.log('auth retry policy tests passed');
}

void main();
