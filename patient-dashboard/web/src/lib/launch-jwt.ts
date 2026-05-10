import { createHmac, timingSafeEqual } from "node:crypto";

/**
 * HS256 launch token shared with the OpenEMR module
 * (interface/modules/custom_modules/oe-module-patient-dashboard-port).
 *
 * The module verifies the user's EMR session + ACL, then mints this
 * token so the dashboard can establish its own session without bouncing
 * the user through OpenEMR's separate OAuth login form. The shared
 * secret lives in DASHBOARD_LAUNCH_SECRET on both services.
 *
 * Replay protection: tokens are 60s and `jti` is held in an in-memory
 * Set for the process lifetime. A single redeploy resets the cache, but
 * 60s TTL bounds the exposure window.
 */

export interface LaunchClaims {
  sub: string;
  pid: number;
  iat: number;
  exp: number;
  jti: string;
}

const seenJti = new Set<string>();
const SEEN_JTI_MAX = 1024;

function base64UrlDecode(input: string): Buffer {
  const padded = input.replace(/-/g, "+").replace(/_/g, "/");
  const pad = padded.length % 4 === 0 ? "" : "=".repeat(4 - (padded.length % 4));
  return Buffer.from(padded + pad, "base64");
}

export function verifyLaunchToken(
  token: string,
  secret: string,
): LaunchClaims {
  if (secret.length === 0) {
    throw new Error("DASHBOARD_LAUNCH_SECRET is not configured");
  }
  const parts = token.split(".");
  if (parts.length !== 3) {
    throw new Error("Malformed launch token");
  }
  const [encodedHeader, encodedPayload, encodedSig] = parts;
  const expected = createHmac("sha256", secret)
    .update(`${encodedHeader}.${encodedPayload}`)
    .digest();
  const provided = base64UrlDecode(encodedSig);
  if (
    expected.length !== provided.length ||
    !timingSafeEqual(expected, provided)
  ) {
    throw new Error("Launch token signature mismatch");
  }
  const header = JSON.parse(base64UrlDecode(encodedHeader).toString("utf8"));
  if (header?.alg !== "HS256") {
    throw new Error(`Unexpected JWT alg: ${header?.alg}`);
  }
  const payload = JSON.parse(
    base64UrlDecode(encodedPayload).toString("utf8"),
  ) as LaunchClaims;
  const now = Math.floor(Date.now() / 1000);
  if (typeof payload.exp !== "number" || payload.exp < now) {
    throw new Error("Launch token expired");
  }
  if (typeof payload.iat !== "number" || payload.iat > now + 30) {
    throw new Error("Launch token iat in the future");
  }
  if (typeof payload.sub !== "string" || payload.sub.length === 0) {
    throw new Error("Launch token missing sub");
  }
  if (typeof payload.pid !== "number" || payload.pid <= 0) {
    throw new Error("Launch token missing pid");
  }
  if (typeof payload.jti !== "string" || payload.jti.length === 0) {
    throw new Error("Launch token missing jti");
  }
  if (seenJti.has(payload.jti)) {
    throw new Error("Launch token replayed");
  }
  if (seenJti.size >= SEEN_JTI_MAX) {
    seenJti.clear();
  }
  seenJti.add(payload.jti);
  return payload;
}
