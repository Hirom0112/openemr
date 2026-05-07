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
    const loginUrl = new URL("/login", req.nextUrl.origin);
    loginUrl.searchParams.set("callbackUrl", pathname);
    return Response.redirect(loginUrl);
  }
});

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
