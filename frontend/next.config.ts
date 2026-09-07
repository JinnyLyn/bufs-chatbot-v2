import type { NextConfig } from "next";

// The /api rewrite target is fixed at BUILD time (standalone output serializes the
// config). On a non-default backend port, build with BACKEND_ORIGIN=http://localhost:<port>.
const backendOrigin = process.env.BACKEND_ORIGIN || "http://localhost:8000";

// Content-Security-Policy.
//
// The chat pane renders model output, and model output is attacker-influenceable via
// prompt injection, so it is treated as hostile. The primary sink is already closed —
// ChatMessage.tsx feeds it to react-markdown with no rehype-raw, so embedded HTML is
// escaped rather than parsed, and react-markdown's default urlTransform drops
// javascript:/data: hrefs — this is the second layer behind that.
//
// script-src keeps 'unsafe-inline' because Next.js injects inline bootstrap and
// streaming-payload scripts; removing it requires per-request nonces threaded through
// middleware, which is worth doing but is not a config-only change. The directives that
// cost nothing are set strictly: connect-src 'self' means an injected script cannot
// exfiltrate a conversation to another origin, frame-ancestors 'none' blocks
// clickjacking, and object-src/base-uri/form-action close the usual bypasses.
const csp = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "connect-src 'self'",
  "frame-ancestors 'none'",
  "frame-src 'none'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "upgrade-insecure-requests",
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: csp },
  // Redundant with frame-ancestors for modern browsers, kept for older ones.
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  // Do not leak the conversation URL to third parties.
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  // The UI needs none of these.
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), interest-cohort=()" },
  // Cloudflare terminates TLS in front of this origin; the header instructs browsers
  // never to try maruvis.kr over plaintext. No preload — that is a one-way door and
  // should be a deliberate decision, not a side effect of this change.
  { key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" },
];

const nextConfig: NextConfig = {
  output: "standalone",
  // Do not advertise the framework version to scanners.
  poweredByHeader: false,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  // 2026-09 개편으로 한국어 단일 서비스가 됐다. 북마크·공유 링크로 /en/chat 에 들어온 학생이
  // 돌아올 길이 없으므로 한국어 화면으로 보낸다(영어 문구 테이블은 코드에만 남아 있다).
  async redirects() {
    return [{ source: "/en/:path*", destination: "/ko/:path*", permanent: false }];
  },
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
