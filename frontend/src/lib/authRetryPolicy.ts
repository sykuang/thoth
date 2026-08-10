type SessionIdentity = {
  serverUrl: string;
  email: string;
};

function normalizeServerUrl(value: string): string {
  return value.trim().replace(/\/+$/, '');
}

export function storedCredentialsMatchSession(
  stored: SessionIdentity,
  active: SessionIdentity,
): boolean {
  return normalizeServerUrl(stored.serverUrl) === normalizeServerUrl(active.serverUrl)
    && stored.email.trim().toLowerCase() === active.email.trim().toLowerCase();
}

export class SessionPromiseGate<T> {
  private active = new Map<string, Promise<T>>();

  getOrStart(key: string, start: () => Promise<T>): Promise<T> {
    const active = this.active.get(key);
    if (active) return active;

    const promise = start();
    this.active.set(key, promise);
    const clear = () => {
      if (this.active.get(key) === promise) this.active.delete(key);
    };
    promise.then(clear, clear);
    return promise;
  }
}
