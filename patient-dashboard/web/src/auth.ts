import NextAuth from "next-auth";
import OpenEMR from "@/lib/auth/openemr-provider";

const baseUrl = process.env.OPENEMR_BASE_URL ?? "https://localhost:9300";

/**
 * Refresh the access token against OpenEMR's token endpoint.
 * Uses client_secret_post auth (matches the registered client metadata).
 */
async function refreshAccessToken(refreshToken: string): Promise<{
  access_token: string;
  refresh_token?: string;
  expires_in: number;
}> {
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: refreshToken,
    client_id: process.env.OPENEMR_OAUTH_CLIENT_ID ?? "",
    client_secret: process.env.OPENEMR_OAUTH_CLIENT_SECRET ?? "",
  });

  const response = await fetch(`${baseUrl}/oauth2/default/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });

  if (!response.ok) {
    const errBody = await response.text();
    throw new Error(`Refresh failed (${response.status}): ${errBody}`);
  }

  return response.json();
}

// SameSite=None cookies are required when this app is loaded inside an
// OpenEMR iframe at a different origin. Auth.js's default ('lax') causes
// the OAuth state cookie to be dropped on the third-party callback,
// producing 'InvalidCheck: state value could not be parsed' errors after
// a successful OpenEMR login. Secure=true is mandatory whenever
// SameSite=None — Railway's edge terminates TLS so this is always true
// in prod. Local-dev (http://localhost) callers fall through to the
// Auth.js defaults via the env guard below.
// `partitioned: true` is required for Chrome 118+ Privacy Sandbox / CHIPS:
// SameSite=None+Secure alone is NOT sufficient when this app runs in a
// third-party iframe. Without partitioning, the OAuth state and pkce
// cookies are dropped on the inbound callback → Auth.js fails with
// 'InvalidCheck: state value could not be parsed' → user sees the
// generic 'Server error / problem with server configuration' UI.
// Safari/older Firefox may not honor `partitioned`; for those a
// pop-out OAuth flow or new-tab-from-OE menu is the fallback.
const crossSiteCookies = process.env.NEXTAUTH_URL?.startsWith("https://")
  ? {
      sessionToken: { options: { sameSite: "none" as const, secure: true, partitioned: true } },
      callbackUrl: { options: { sameSite: "none" as const, secure: true, partitioned: true } },
      csrfToken: { options: { sameSite: "none" as const, secure: true, partitioned: true } },
      pkceCodeVerifier: { options: { sameSite: "none" as const, secure: true, partitioned: true } },
      state: { options: { sameSite: "none" as const, secure: true, partitioned: true } },
      nonce: { options: { sameSite: "none" as const, secure: true, partitioned: true } },
    }
  : undefined;

export const { handlers, auth, signIn, signOut } = NextAuth({
  // Explicit basePath for Auth.js action parsing. Without this, Auth.js
  // derives basePath from NEXTAUTH_URL's path component — which here is
  // `/dashboard/api/auth` because NEXTAUTH_URL must include the same-origin
  // `/dashboard` prefix for OAuth callback URL generation. But Next.js's
  // own `basePath: "/dashboard"` (next.config.ts) strips `/dashboard` from
  // inbound URLs before this handler runs, so Auth.js sees `/api/auth/...`.
  // Two layers of basePath stripping that disagree → every action 400s
  // with `UnknownAction: Cannot parse action at /api/auth/session`. Pinning
  // basePath here to the post-strip path keeps action parsing correct
  // while NEXTAUTH_URL keeps the `/dashboard` prefix for callback URLs.
  basePath: "/api/auth",
  providers: [
    OpenEMR({
      baseUrl,
      clientId: process.env.OPENEMR_OAUTH_CLIENT_ID,
      clientSecret: process.env.OPENEMR_OAUTH_CLIENT_SECRET,
    }),
  ],
  session: { strategy: "jwt" },
  pages: { signIn: "/login" },
  ...(crossSiteCookies ? { cookies: crossSiteCookies } : {}),
  callbacks: {
    async jwt({ token, account, profile }) {
      // Initial sign-in: persist OAuth tokens + fhirUser claim.
      if (account) {
        token.accessToken = account.access_token;
        token.refreshToken = account.refresh_token;
        token.expiresAt =
          typeof account.expires_at === "number"
            ? account.expires_at
            : Math.floor(Date.now() / 1000) + 3600;
        if (profile?.fhirUser) {
          token.fhirUser = profile.fhirUser;
        }
        return token;
      }

      // Token still valid: passthrough.
      const now = Math.floor(Date.now() / 1000);
      if (typeof token.expiresAt === "number" && now < token.expiresAt - 30) {
        return token;
      }

      // Expired: try to refresh. On any failure (no refresh token,
      // refresh request rejected, network error), invalidate the JWT
      // by returning null. Auth.js treats this as a logged-out state →
      // proxy.ts redirects /patient/* → /login → the OpenEMR OAuth
      // provider re-authenticates silently if the user still has an
      // OpenEMR session, producing a fresh access_token. This avoids
      // the previous failure mode where we returned a token-without-
      // accessToken and FHIR calls 401'd one card at a time.
      const refreshToken = token.refreshToken;
      if (typeof refreshToken !== "string" || refreshToken.length === 0) {
        console.warn("[auth] Access token expired and no refresh token; clearing session");
        return null;
      }

      try {
        const refreshed = await refreshAccessToken(refreshToken);
        token.accessToken = refreshed.access_token;
        token.expiresAt = Math.floor(Date.now() / 1000) + refreshed.expires_in;
        if (refreshed.refresh_token) {
          token.refreshToken = refreshed.refresh_token;
        }
        delete token.error;
        return token;
      } catch (error) {
        console.error("[auth] Failed to refresh access token; clearing session", error);
        return null;
      }
    },
    async session({ session, token }) {
      const accessToken = token.accessToken;
      const fhirUser = token.fhirUser;
      const tokenError = token.error;
      session.accessToken =
        typeof accessToken === "string" ? accessToken : undefined;
      session.fhirUser = typeof fhirUser === "string" ? fhirUser : null;
      if (tokenError === "RefreshAccessTokenError") {
        session.error = tokenError;
      }
      if (session.user) {
        session.user.fhirUser =
          typeof fhirUser === "string" ? fhirUser : null;
      }
      return session;
    },
  },
});
