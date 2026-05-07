import type { NextConfig } from "next";
import path from "node:path";

const nextConfig: NextConfig = {
    // Pin Turbopack's workspace root to this app to avoid Next picking up
    // the parent OpenEMR repo's lockfile.
    turbopack: {
        root: path.resolve(__dirname),
    },
};

export default nextConfig;
