#!/usr/bin/env bash
# =============================================================================
# mint_demo_jwt.sh
#
# Mints a short-lived (5 minute) HS256 JWT for the W2 demo.
# Token shape matches what JwtMinter.php / agent-api/auth issue for the
# React iframe and the custom upload endpoint.
#
# Usage:
#   # 1) source the secret without echoing it
#   set -a; source docker/development-easy/.env.copilot; set +a
#
#   # 2) mint and capture
#   TOKEN="$(./scripts/mint_demo_jwt.sh)"
#
#   # 3) use it
#   curl -H "Authorization: Bearer $TOKEN" ...
#
# Env vars:
#   COPILOT_JWT_SECRET   (required)  HS256 shared secret, >= 32 chars.
#   DEMO_JWT_SUB         (optional)  subject claim, default "1"
#   DEMO_JWT_SID         (optional)  session id claim, default "demo"
#   DEMO_JWT_ISS         (optional)  issuer, default "openemr-copilot"
#   DEMO_JWT_TTL         (optional)  lifetime in seconds, default 300
#
# Security:
#   - The secret is read from the environment only. It is never echoed,
#     written to disk, or included in error messages.
#   - On any failure the script exits non-zero with a generic message.
# =============================================================================

set -euo pipefail

if [[ -z "${COPILOT_JWT_SECRET:-}" ]]; then
  echo "error: COPILOT_JWT_SECRET is not set in the environment" >&2
  echo "hint:  set -a; source docker/development-easy/.env.copilot; set +a" >&2
  exit 1
fi

SUB="${DEMO_JWT_SUB:-1}"
SID="${DEMO_JWT_SID:-demo}"
ISS="${DEMO_JWT_ISS:-openemr-copilot}"
TTL="${DEMO_JWT_TTL:-300}"

# Prefer Python (always present alongside agent-api). Fall back to python3.
PY="$(command -v python3 || command -v python || true)"
if [[ -z "$PY" ]]; then
  echo "error: python3 is required to mint the demo JWT" >&2
  exit 1
fi

# Mint via stdlib only — no PyJWT dependency, no third-party imports.
# The secret is passed via env, never via argv (argv is visible in `ps`).
COPILOT_JWT_SECRET="$COPILOT_JWT_SECRET" \
DEMO_JWT_SUB="$SUB" DEMO_JWT_SID="$SID" DEMO_JWT_ISS="$ISS" DEMO_JWT_TTL="$TTL" \
"$PY" - <<'PY'
import base64, hmac, hashlib, json, os, time, sys

def b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")

secret = os.environ["COPILOT_JWT_SECRET"].encode("utf-8")
if len(secret) < 32:
    print("error: COPILOT_JWT_SECRET must be at least 32 characters", file=sys.stderr)
    sys.exit(1)

now = int(time.time())
ttl = int(os.environ.get("DEMO_JWT_TTL", "300"))

header = {"alg": "HS256", "typ": "JWT"}
payload = {
    "sub": os.environ.get("DEMO_JWT_SUB", "1"),
    "sid": os.environ.get("DEMO_JWT_SID", "demo"),
    "iss": os.environ.get("DEMO_JWT_ISS", "openemr-copilot"),
    "iat": now,
    "exp": now + ttl,
}

h = b64url(json.dumps(header,  separators=(",", ":")).encode("utf-8"))
p = b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
signing_input = h + b"." + p
sig = b64url(hmac.new(secret, signing_input, hashlib.sha256).digest())

sys.stdout.write((signing_input + b"." + sig).decode("ascii"))
sys.stdout.write("\n")
PY
