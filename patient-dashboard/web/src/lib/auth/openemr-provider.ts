import type { OAuthConfig, OAuthUserConfig } from "next-auth/providers";

/**
 * OpenEMR SMART-on-FHIR OAuth2 / OIDC provider for Auth.js v5.
 *
 * Configured against the local OpenEMR docker (https://localhost:9300).
 * Uses authorization-code flow with a confidential client registered via
 * RFC 7591 dynamic client registration. See ../../../auth-notes.md.
 */

export interface OpenEMRProfile {
  sub: string;
  fhirUser?: string;
  email?: string;
  email_verified?: boolean;
  name?: string;
  given_name?: string;
  family_name?: string;
  preferred_username?: string;
  [claim: string]: unknown;
}

const SCOPES = [
  "openid",
  "fhirUser",
  "offline_access",
  "user/Patient.read",
  "user/AllergyIntolerance.read",
  "user/Condition.read",
  "user/MedicationRequest.read",
  "user/CareTeam.read",
  "user/Observation.read",
].join(" ");

export default function OpenEMR<P extends OpenEMRProfile>(
  options: OAuthUserConfig<P> & { baseUrl: string },
): OAuthConfig<P> {
  const { baseUrl, ...rest } = options;
  return {
    id: "openemr",
    name: "OpenEMR",
    type: "oidc",
    issuer: `${baseUrl}/oauth2/default`,
    wellKnown: `${baseUrl}/oauth2/default/.well-known/openid-configuration`,
    authorization: {
      url: `${baseUrl}/oauth2/default/authorize`,
      params: { scope: SCOPES },
    },
    token: `${baseUrl}/oauth2/default/token`,
    userinfo: `${baseUrl}/oauth2/default/userinfo`,
    jwks_endpoint: `${baseUrl}/oauth2/default/jwk`,
    idToken: true,
    checks: ["pkce", "state"],
    client: {
      token_endpoint_auth_method: "client_secret_post",
    },
    profile(profile) {
      return {
        id: profile.sub,
        name:
          profile.name ||
          [profile.given_name, profile.family_name].filter(Boolean).join(" ") ||
          profile.preferred_username ||
          profile.sub,
        email: profile.email ?? null,
        fhirUser: profile.fhirUser ?? null,
      };
    },
    ...rest,
  };
}
