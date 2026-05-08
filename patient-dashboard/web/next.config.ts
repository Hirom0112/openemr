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
    // Pin Turbopack's workspace root to this app to avoid Next picking up
    // the parent OpenEMR repo's lockfile.
    turbopack: {
        root: path.resolve(__dirname),
    },
};

export default nextConfig;
