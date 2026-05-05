/**
 * JWT token holder + silent-refresh helper for the Clinical Co-Pilot iframe.
 *
 * Storage policy
 * --------------
 * The token lives in a single module-level variable — never localStorage or
 * sessionStorage. The PHP-minted token has an 8-hour TTL and the demo's XSS
 * surface area is non-trivial (Smarty templates, third-party JS bundled into
 * the iframe), so persisting it would be a net loss vs. simply re-asking the
 * parent for a fresh token whenever this iframe reloads.
 *
 * Refresh flow
 * ------------
 * On 401 we postMessage `copilot:request-refresh-jwt` to `window.parent` and
 * await a `copilot:jwt` reply. If no fresh token arrives within
 * REFRESH_TIMEOUT_MS, we surface a toast and reject the original request.
 * The parent-side handler does not yet exist — see report (and the TODO note
 * for JwtMinter.php's docblock) for the follow-up PR.
 */

const REFRESH_TIMEOUT_MS = 3000;

let currentToken: string | null = null;

// Single in-flight refresh promise — concurrent 401s coalesce onto one
// postMessage round-trip rather than racing the parent for N tokens.
let pendingRefresh: Promise<string | null> | null = null;

// Subscribers notified when refresh fails. ChatSurface mounts a toast that
// listens here. Kept as a Set so multiple components could subscribe (only
// one does today).
type ToastListener = (message: string) => void;
const toastListeners = new Set<ToastListener>();

/** Seed the in-memory holder from the PHP-injected config block. */
export function initAuthToken(token: string | undefined | null): void {
  currentToken = typeof token === 'string' && token.length > 0 ? token : null;
}

/** Test/diagnostic accessor — exported so devtools can confirm seed worked. */
export function getAuthToken(): string | null {
  return currentToken;
}

/** Test-only: reset module state between cases. */
export function _resetAuthForTests(): void {
  currentToken = null;
  pendingRefresh = null;
  toastListeners.clear();
}

/**
 * Clone `init` and add `Authorization: Bearer <token>` when a token is held.
 * Returns the original init unchanged when no token is set — preserves the
 * "auth disabled" path for local dev where COPILOT_JWT_SECRET is empty.
 */
export function withAuth(init?: RequestInit): RequestInit {
  if (!currentToken) {
    return init ?? {};
  }
  const next: RequestInit = { ...(init ?? {}) };
  // Headers can be a Headers instance, a tuple list, or a plain record.
  // Normalize to a plain record so we can splice in Authorization without
  // disturbing existing entries (Content-Type, X-Request-ID).
  const merged: Record<string, string> = {};
  const src = init?.headers;
  if (src instanceof Headers) {
    src.forEach((v, k) => { merged[k] = v; });
  } else if (Array.isArray(src)) {
    for (const [k, v] of src) merged[k] = v;
  } else if (src && typeof src === 'object') {
    Object.assign(merged, src as Record<string, string>);
  }
  merged['Authorization'] = `Bearer ${currentToken}`;
  next.headers = merged;
  return next;
}

export function subscribeToast(listener: ToastListener): () => void {
  toastListeners.add(listener);
  return () => { toastListeners.delete(listener); };
}

function emitToast(message: string): void {
  for (const l of toastListeners) {
    try { l(message); } catch (err) { console.debug('[copilot] toast listener threw', err); }
  }
}

/**
 * Request a fresh JWT from the parent OpenEMR window. Resolves with the new
 * token (also stored in `currentToken`) or `null` on timeout.
 *
 * Concurrent callers share a single round-trip via `pendingRefresh`.
 */
export function requestRefreshedToken(): Promise<string | null> {
  if (pendingRefresh) return pendingRefresh;
  if (typeof window === 'undefined' || !window.parent) {
    return Promise.resolve(null);
  }
  pendingRefresh = new Promise<string | null>((resolve) => {
    const handler = (event: MessageEvent): void => {
      const data = event.data as { type?: unknown; jwt?: unknown } | null;
      if (!data || data.type !== 'copilot:jwt' || typeof data.jwt !== 'string') {
        return;
      }
      cleanup();
      currentToken = data.jwt;
      resolve(data.jwt);
    };
    const timer = window.setTimeout(() => {
      cleanup();
      resolve(null);
    }, REFRESH_TIMEOUT_MS);
    const cleanup = (): void => {
      window.removeEventListener('message', handler);
      window.clearTimeout(timer);
      // Allow the next 401 to kick off a fresh round-trip.
      pendingRefresh = null;
    };
    window.addEventListener('message', handler);
    try {
      // Parent-origin restriction is intentionally relaxed for the demo; the
      // production deployment will narrow this to the OpenEMR origin.
      window.parent.postMessage({ type: 'copilot:request-refresh-jwt' }, '*');
    } catch (err) {
      console.warn('[copilot] postMessage to parent failed', err);
      cleanup();
      resolve(null);
    }
  });
  return pendingRefresh;
}

/** Surface the "session expired" toast to any subscribed UI. */
export function notifyAuthFailure(message = 'Session expired. Please reload OpenEMR.'): void {
  emitToast(message);
}
