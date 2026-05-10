import type { DefaultSession } from "next-auth";
import type { JWT as DefaultJWT } from "next-auth/jwt";

// Re-export to keep TS aware that the module is augmentable.
export type { DefaultJWT };

declare module "next-auth" {
  interface Session {
    accessToken?: string;
    fhirUser?: string | null;
    error?: "RefreshAccessTokenError";
    user?: DefaultSession["user"] & {
      fhirUser?: string | null;
    };
  }

  interface User {
    fhirUser?: string | null;
    /** Set by the `launch` Credentials provider (see auth.ts). */
    launchAccessToken?: string;
    launchRefreshToken?: string;
    launchExpiresAt?: number;
  }

  interface Profile {
    fhirUser?: string;
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    accessToken?: string;
    refreshToken?: string;
    expiresAt?: number;
    fhirUser?: string | null;
    error?: "RefreshAccessTokenError";
  }
}

export {};
