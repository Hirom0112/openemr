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
const crossSiteCookies = process.env.NEXTAUTH_URL?.startsWith("https://")
  ? {
      sessionToken: { options: { sameSite: "none" as const, secure: true } },
      callbackUrl: { options: { sameSite: "none" as const, secure: true } },
      csrfToken: { options: { sameSite: "none" as const, secure: true } },
      pkceCodeVerifier: { options: { sameSite: "none" as const, secure: true } },
      state: { options: { sameSite: "none" as const, secure: true } },
      nonce: { options: { sameSite: "none" as const, secure: true } },
    }
  : undefined;

export const { handlers, auth, signIn, signOut } = NextAuth({
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

      // Expired: try to refresh.
      const refreshToken = token.refreshToken;
      if (typeof refreshToken !== "string" || refreshToken.length === 0) {
        return { ...token, error: "RefreshAccessTokenError" };
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
        console.error("[auth] Failed to refresh access token", error);
        return { ...token, error: "RefreshAccessTokenError" };
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
