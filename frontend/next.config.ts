import type { NextConfig } from "next";

// The /api rewrite target is fixed at BUILD time (standalone output serializes the
// config). On a non-default backend port, build with BACKEND_ORIGIN=http://localhost:<port>.
const backendOrigin = process.env.BACKEND_ORIGIN || "http://localhost:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${backendOrigin}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
