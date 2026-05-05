import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

// jwt.ts touches `window` — stub a minimal surface (addEventListener,
// removeEventListener, setTimeout/clearTimeout, parent.postMessage) before
// importing the module so the module-scope guard sees a usable window.
// Avoids a hard dependency on jsdom (not installed in this workspace).
type Listener = (event: { data: unknown }) => void;
const listeners = new Set<Listener>();

const fakeWindow = {
  addEventListener: (type: string, l: Listener): void => {
    if (type === 'message') listeners.add(l);
  },
  removeEventListener: (type: string, l: Listener): void => {
    if (type === 'message') listeners.delete(l);
  },
  setTimeout: ((fn: () => void, ms: number) => globalThis.setTimeout(fn, ms)) as Window['setTimeout'],
  clearTimeout: ((id: number) => globalThis.clearTimeout(id)) as Window['clearTimeout'],
  parent: {
    postMessage: vi.fn(),
  },
};

(globalThis as { window?: unknown }).window = fakeWindow;

const dispatchMessage = (data: unknown): void => {
  for (const l of listeners) l({ data });
};

const {
  initAuthToken,
  getAuthToken,
  withAuth,
  requestRefreshedToken,
  notifyAuthFailure,
  subscribeToast,
  _resetAuthForTests,
} = await import('./jwt');

describe('jwt auth module', () => {
  beforeEach(() => {
    _resetAuthForTests();
    listeners.clear();
    fakeWindow.parent.postMessage.mockClear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('withAuth attaches Authorization when a token is held', () => {
    initAuthToken('eyJhbGciOiJIUzI1NiJ9.payload.sig');
    const init = withAuth({ method: 'POST', headers: { 'Content-Type': 'application/json' } });
    const headers = init.headers as Record<string, string>;
    expect(headers['Authorization']).toBe('Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig');
    expect(headers['Content-Type']).toBe('application/json');
    expect(init.method).toBe('POST');
  });

  it('withAuth returns header-less init when no token is set (auth-disabled dev path)', () => {
    initAuthToken(undefined);
    const original: RequestInit = { method: 'GET', headers: { 'X-Request-ID': 'abc' } };
    const init = withAuth(original);
    const headers = (init.headers ?? {}) as Record<string, string>;
    expect(headers['Authorization']).toBeUndefined();
    expect(headers['X-Request-ID']).toBe('abc');
  });

  it('getAuthToken exposes the seeded 3-segment token (devtools verification)', () => {
    initAuthToken('a.b.c');
    const t = getAuthToken();
    expect(t).toBe('a.b.c');
    expect(t?.split('.').length).toBe(3);
  });

  it('401 → requestRefreshedToken posts to parent and resolves with replied token', async () => {
    initAuthToken('stale.token.x');
    const promise = requestRefreshedToken();
    // Let the promise install its message listener before we dispatch.
    await Promise.resolve();
    dispatchMessage({ type: 'copilot:jwt', jwt: 'fresh.token.y' });
    const refreshed = await promise;
    expect(refreshed).toBe('fresh.token.y');
    expect(getAuthToken()).toBe('fresh.token.y');
    expect(fakeWindow.parent.postMessage).toHaveBeenCalledWith(
      { type: 'copilot:request-refresh-jwt' },
      '*',
    );
  });

  it('401 + refresh timeout → toast subscriber is fired', async () => {
    vi.useFakeTimers();
    const seen: string[] = [];
    subscribeToast((m) => seen.push(m));

    const promise = requestRefreshedToken();
    // No reply ever arrives — advance past the 3s timeout.
    await vi.advanceTimersByTimeAsync(3500);
    const result = await promise;
    expect(result).toBeNull();

    // The api.ts caller invokes notifyAuthFailure when refresh returns null.
    notifyAuthFailure();
    expect(seen.length).toBe(1);
    expect(seen[0]).toMatch(/Session expired/i);
  });
});
