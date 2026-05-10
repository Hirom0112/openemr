import NextAuth from "next-auth";
import Credentials from "next-auth/providers/credentials";
import OpenEMR from "@/lib/auth/openemr-provider";
import { verifyLaunchToken } from "@/lib/launch-jwt";

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

/**
 * Password-grant token exchange used by the SSO launch bridge. The
 * launch JWT proves the user already authenticated against OpenEMR
 * (the PHP module verified the session + ACL before minting the JWT),
 * but the dashboard still needs a real OpenEMR access token to call
 * FHIR APIs. We exchange via the password grant against a service
 * account configured by `DASHBOARD_LAUNCH_OAUTH_USER` /
 * `DASHBOARD_LAUNCH_OAUTH_PASSWORD`. OpenEMR must have
 * `oauth_password_grant` enabled.
 */
async function exchangeLaunchForAccessToken(): Promise<{
  access_token: string;
  refresh_token?: string;
  expires_in: number;
}> {
  const user = process.env.DASHBOARD_LAUNCH_OAUTH_USER ?? "";
  const password = process.env.DASHBOARD_LAUNCH_OAUTH_PASSWORD ?? "";
  if (user === "" || password === "") {
    throw new Error(
      "DASHBOARD_LAUNCH_OAUTH_USER / DASHBOARD_LAUNCH_OAUTH_PASSWORD not configured",
    );
  }
  const body = new URLSearchParams({
    grant_type: "password",
    username: user,
    password,
    scope:
      "openid fhirUser offline_access user/Patient.read user/AllergyIntolerance.read user/Condition.read user/MedicationRequest.read user/CareTeam.read user/Observation.read user/Binary.read user/DocumentReference.read",
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
    throw new Error(`Launch token exchange failed (${response.status}): ${errBody}`);
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
    Credentials({
      id: "launch",
      name: "OpenEMR Launch JWT",
      credentials: {
        launch: { label: "Launch token", type: "text" },
      },
      // Verifies the HS256 launch JWT minted by the OpenEMR module
      // and exchanges for an OpenEMR access token via password grant.
      // We stash the token on the returned user object; the jwt
      // callback below promotes it into the session JWT (matching the
      // shape used by the OAuth provider so the rest of the app can
      // treat both flows identically).
      async authorize(credentials) {
        const launch = credentials?.launch;
        if (typeof launch !== "string" || launch.length === 0) {
          return null;
        }
        const secret = process.env.DASHBOARD_LAUNCH_SECRET ?? "";
        try {
          const claims = verifyLaunchToken(launch, secret);
          const tokens = await exchangeLaunchForAccessToken();
          const expiresAt =
            Math.floor(Date.now() / 1000) + tokens.expires_in;
          return {
            id: claims.sub,
            name: `OpenEMR user ${claims.sub}`,
            // Stashed for the jwt callback. Not part of the standard
            // User contract — augmented in src/types/next-auth.d.ts.
            launchAccessToken: tokens.access_token,
            launchRefreshToken: tokens.refresh_token,
            launchExpiresAt: expiresAt,
          };
        } catch (err) {
          console.error("[auth/launch] handshake rejected", err);
          return null;
        }
      },
    }),
  ],
  session: { strategy: "jwt" },
  pages: { signIn: "/login" },
  ...(crossSiteCookies ? { cookies: crossSiteCookies } : {}),
  callbacks: {
    async jwt({ token, account, profile, user }) {
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

      // Credentials sign-in via the launch bridge: tokens are stashed
      // on the user object (Credentials providers do not produce an
      // `account`). Promote them to the JWT shape used by the OAuth
      // provider so downstream code is provider-agnostic.
      if (user && typeof user.launchAccessToken === "string") {
        token.accessToken = user.launchAccessToken;
        if (typeof user.launchRefreshToken === "string") {
          token.refreshToken = user.launchRefreshToken;
        }
        if (typeof user.launchExpiresAt === "number") {
          token.expiresAt = user.launchExpiresAt;
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
