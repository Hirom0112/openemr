import type { NextConfig } from "next";
import path from "node:path";

// basePath puts every dashboard route under /dashboard so Apache on the
// OpenEMR container can ProxyPass `/dashboard/*` here while keeping the
// browser at the OpenEMR origin. Same-origin = no third-party-cookie
// nonsense for the iframe-embedded OAuth flow. Auth.js automatically
// honours basePath when generating callback URLs (NEXTAUTH_URL must
// include the same `/dashboard` suffix). The leading slash is required.
const nextConfig: NextConfig = {
    basePath: "/dashboard",
    // Standalone output is required for `basePath` to work end-to-end in
    // production. Without it, Next 16 / Turbopack registers routes
    // correctly in routes-manifest.json (basePath: "/dashboard", page:
    // "/login") but `next start` serves them at the unprefixed path
    // (`/login` works, `/dashboard/login` 404s). Standalone emits a
    // bundled `server.js` that handles basePath stripping properly.
    output: "standalone",
    // Pin Turbopack's workspace root to this app to avoid Next picking up
    // the parent OpenEMR repo's lockfile.
    turbopack: {
        root: path.resolve(__dirname),
    },
};

export default nextConfig;
