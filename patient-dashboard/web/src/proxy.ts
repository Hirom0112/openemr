import { auth } from "@/auth";

/**
 * Auth.js v5 route protection. Replaces the deprecated `middleware.ts`
 * convention with Next 16's `proxy.ts` convention.
 *
 * Auth.js exports an `auth` higher-order handler whose signature works
 * identically under proxy.ts as it did under middleware.ts.
 */
export default auth((req) => {
  const { pathname } = req.nextUrl;

  // Public paths and Next.js / Auth.js internals.
  if (
    pathname === "/" ||
    pathname.startsWith("/login") ||
    pathname.startsWith("/api/auth") ||
    pathname.startsWith("/_next") ||
    pathname.startsWith("/favicon")
  ) {
    return;
  }

  // Protect /patient/*.
  if (pathname.startsWith("/patient") && !req.auth) {
    // SSO launch bridge: when OpenEMR redirects the iframe with a
    // ?launch=<jwt> query param, hand the request to /api/launch
    // instead of bouncing through the OAuth login form. /api/launch
    // verifies the HS256 JWT against DASHBOARD_LAUNCH_SECRET and
    // exchanges for an access token via the password grant.
    const launch = req.nextUrl.searchParams.get("launch");
    if (launch) {
      const launchUrl = new URL("/api/launch", req.nextUrl.origin);
      launchUrl.searchParams.set("launch", launch);
      // Preserve the patient pid from the path so the launch endpoint
      // can build a safe redirect target.
      const pidMatch = pathname.match(/^\/patient\/([0-9]+)/);
      if (pidMatch) {
        launchUrl.searchParams.set("pid", pidMatch[1]);
      }
      return Response.redirect(launchUrl);
    }
    const loginUrl = new URL("/login", req.nextUrl.origin);
    loginUrl.searchParams.set("callbackUrl", pathname);
    return Response.redirect(loginUrl);
  }
});

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
