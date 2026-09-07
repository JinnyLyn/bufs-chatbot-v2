// Outage page Worker for maruvis.kr (reports/CamChat-장애대응.pdf §11–§12).
//
// Normal state: every request is passed to the origin untouched — including the AI
// answer stream, which is returned as the origin's own streaming body (no buffering).
//
// When a student's *page* request cannot reach a working origin (fetch throws, or the
// edge/tunnel answers with a 5xx), they get a Korean outage page with contacts instead
// of Cloudflare's English error screen. The page is a real HTTP 503 with Retry-After so
// external monitors still see the outage.
//
// API, streaming, Next.js internals and static assets are NEVER replaced with HTML: the
// frontend expects JSON/SSE on those paths and handles their failures itself.
//
// Maintenance mode: KV key `maintenance` = "on" (binding OUTAGE) serves the maintenance
// page for page requests without a redeploy. Missing binding / KV error = mode off.

import { outagePage, maintenancePage } from "./pages.js";

const DEFAULT_RETRY_AFTER_S = 300;

// Statuses that mean "the origin did not answer this request", from the origin itself
// (502/503/504) or from Cloudflare's edge in front of the tunnel (52x/530).
export const ORIGIN_DOWN_STATUSES = new Set([502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530]);

const STATIC_EXT = /\.(?:js|mjs|css|map|png|jpe?g|gif|webp|svg|ico|woff2?|ttf|otf|json|txt|xml|webmanifest)$/i;
// Navigations that render a whole page: a top-level document, or the chat embedded in a frame.
const PAGE_DESTS = new Set(["document", "iframe", "frame"]);

/** Only top-level page loads get the HTML page. Everything else is passed through as-is. */
export function isPageRequest(request) {
  if (request.method !== "GET" && request.method !== "HEAD") return false;
  const { pathname } = new URL(request.url);
  if (pathname.startsWith("/api/") || pathname.startsWith("/_next/")) return false;
  if (STATIC_EXT.test(pathname)) return false;
  const dest = request.headers.get("sec-fetch-dest");
  if (dest) return PAGE_DESTS.has(dest); // modern browsers state it outright
  return (request.headers.get("accept") || "").includes("text/html");
}

function retryAfterSeconds(env) {
  const n = Number(env.RETRY_AFTER_S);
  // Anything but a positive whole number of seconds is a config typo: fall back, don't coerce.
  return Number.isInteger(n) && n > 0 ? n : DEFAULT_RETRY_AFTER_S;
}

async function maintenanceOn(env) {
  try {
    if (!env.OUTAGE) return false;
    return (await env.OUTAGE.get("maintenance")) === "on";
  } catch {
    return false; // a KV hiccup must not turn into a fake maintenance page
  }
}

/** Point the request at ORIGIN_HOST (a no-op on the production route, where host == origin). */
export function toOrigin(request, env) {
  const url = new URL(request.url);
  const originHost = env.ORIGIN_HOST;
  if (!originHost || url.host === originHost) return request;
  url.host = originHost;
  url.protocol = "https:";
  return new Request(url.toString(), request);
}

function htmlResponse(body, request, retry, reason) {
  return new Response(request.method === "HEAD" ? null : body, {
    status: 503,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Retry-After": String(retry),
      "Cache-Control": "no-store",
      "X-Robots-Tag": "noindex",
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "no-referrer",
      "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
      "X-CamChat-Outage": reason,
    },
  });
}

/**
 * @param {Request} request
 * @param {object} env  - ORIGIN_HOST, RETRY_AFTER_S, OUTAGE (KV, optional)
 * @param {typeof fetch} originFetch - injectable for tests
 */
export async function handle(request, env, originFetch = fetch) {
  const page = isPageRequest(request);
  const retry = retryAfterSeconds(env);

  if (page && (await maintenanceOn(env))) {
    return htmlResponse(maintenancePage(retry), request, retry, "maintenance");
  }

  let res;
  try {
    res = await originFetch(toOrigin(request, env));
  } catch (err) {
    if (page) return htmlResponse(outagePage(retry), request, retry, "origin-unreachable");
    // API/asset callers get a plain 502; the frontend turns it into its own notice.
    return new Response(JSON.stringify({ detail: "origin unreachable" }), {
      status: 502,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store", "X-CamChat-Outage": "origin-unreachable" },
    });
  }

  if (page && ORIGIN_DOWN_STATUSES.has(res.status)) {
    return htmlResponse(outagePage(retry), request, retry, `origin-${res.status}`);
  }
  return res;
}

export default {
  fetch(request, env) {
    return handle(request, env);
  },
};
