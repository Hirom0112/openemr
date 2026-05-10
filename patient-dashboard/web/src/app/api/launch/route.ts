import { NextResponse, type NextRequest } from "next/server";
import { signIn } from "@/auth";

/**
 * SSO launch handshake. The OpenEMR module hits the iframe with
 * `?launch=<jwt>`; the patient page redirects here on first hit when no
 * Auth.js session exists. We delegate to the `launch` Credentials
 * provider in `@/auth`, which validates the JWT and trades it for an
 * OpenEMR access token via the server-side password grant. Auth.js
 * handles cookie creation; we redirect to the patient page.
 *
 * The redirect URL is whitelisted to `/patient/<id>` (numeric) so a
 * crafted launch token cannot redirect to an attacker-controlled path.
 */
export async function GET(req: NextRequest): Promise<Response> {
  const url = new URL(req.url);
  const launch = url.searchParams.get("launch");
  const pid = url.searchParams.get("pid") ?? "";
  if (!launch) {
    return NextResponse.json({ error: "missing launch" }, { status: 400 });
  }
  const safePid = /^[0-9]+$/.test(pid) ? pid : "4";
  // Include the `/dashboard` basePath because Auth.js's signIn redirect
  // uses the URL verbatim — it has no awareness of Next's basePath.
  const redirectTo = `/dashboard/patient/${safePid}`;
  try {
    // signIn returns a Response (redirect) when redirectTo is set.
    return await signIn("launch", { launch, redirectTo, redirect: true });
  } catch (err) {
    // Next throws a NEXT_REDIRECT error to perform the redirect; let it
    // propagate so the framework handles it.
    if (
      err instanceof Error &&
      "digest" in err &&
      typeof (err as { digest?: unknown }).digest === "string" &&
      (err as { digest: string }).digest.startsWith("NEXT_REDIRECT")
    ) {
      throw err;
    }
    console.error("[launch] handshake failed", err);
    return NextResponse.redirect(new URL("/login", req.url));
  }
}
