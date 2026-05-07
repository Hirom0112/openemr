/**
 * Token provider seam. The FhirClient does not know how the access token is
 * acquired or refreshed; it simply asks a TokenProvider for the current token
 * before each request. Phase 3.2 owns the Auth.js wiring that backs the
 * default `authSessionTokenProvider` below.
 *
 * Important: this module avoids a top-level `import { auth } from "@/auth"`.
 * `@/auth` transitively pulls in `next-auth` -> `next/server`, which fails
 * to resolve under non-Next runtimes (vitest, isolated unit tests, etc.).
 * The Auth.js module is therefore loaded dynamically *inside* the provider's
 * `getAccessToken` call. This keeps the client unit-testable without a Next
 * runtime, while still letting Server Components import this provider
 * directly.
 */

export interface TokenProvider {
  getAccessToken(): Promise<string>;
}

/**
 * Default token provider: reads the Auth.js v5 session via the server-side
 * `auth()` helper exported by `@/auth`.
 *
 * Resolved by Phase 3.2; do not call from a Server Component until then.
 * Calling outside a Next request context will throw.
 */
export const authSessionTokenProvider: TokenProvider = {
  async getAccessToken(): Promise<string> {
    // Dynamic import keeps next-auth out of vitest's module graph.
    const mod: { auth: () => Promise<unknown> } = await import("@/auth");
    const session = (await mod.auth()) as
      | { accessToken?: unknown }
      | null;
    const token = session?.accessToken;
    if (typeof token !== "string" || token.length === 0) {
      throw new Error("No access token on session");
    }
    return token;
  },
};

/**
 * Test helper: build a TokenProvider from a static string. Not for production.
 */
export function staticTokenProvider(token: string): TokenProvider {
  return {
    async getAccessToken(): Promise<string> {
      return token;
    },
  };
}
