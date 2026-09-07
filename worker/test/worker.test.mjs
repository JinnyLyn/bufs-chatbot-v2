// Run: cd worker && npm test  (node's built-in runner; Request/Response come from Node 22+)
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { handle, isPageRequest, toOrigin, _resetMaintenanceMemo } from "../src/index.js";

beforeEach(() => _resetMaintenanceMemo());

const ENV = { ORIGIN_HOST: "maruvis.kr", RETRY_AFTER_S: "300" };
const PAGE = { accept: "text/html,application/xhtml+xml", "sec-fetch-dest": "document" };

const req = (path, init = {}) => new Request(`https://maruvis.kr${path}`, init);
const originReturning = (status, body = "origin", headers = {}) => async () =>
  new Response(body, { status, headers });
const originThrowing = async () => { throw new TypeError("connect failed"); };
const kv = (value) => ({ get: async (k) => (k === "maintenance" ? value : null) });

test("page request + origin 5xx → Korean outage page as a real 503 with Retry-After", async () => {
  for (const status of [502, 503, 504, 521, 522, 530]) {
    const res = await handle(req("/ko/chat", { headers: PAGE }), ENV, originReturning(status));
    assert.equal(res.status, 503, `status ${status}`);
    assert.equal(res.headers.get("Retry-After"), "300");
    assert.equal(res.headers.get("Cache-Control"), "no-store");
    assert.match(res.headers.get("Content-Type"), /text\/html/);
    const body = await res.text();
    assert.match(body, /챗봇 점검 중입니다/);
    assert.match(body, /잠시 후 다시 접속해 주세요/);
    assert.match(body, /051-509-5182~5183/);
    assert.match(body, /Haksa_Iljeong/);
    assert.match(body, /5444/); // department directory is on the page too
  }
});

test("page request + origin unreachable (fetch throws) → outage page", async () => {
  const res = await handle(req("/", { headers: PAGE }), ENV, originThrowing);
  assert.equal(res.status, 503);
  assert.equal(res.headers.get("X-CamChat-Outage"), "origin-unreachable");
  assert.match(await res.text(), /챗봇 점검 중입니다/);
});

test("HEAD page request gets the 503 headers and no body", async () => {
  const res = await handle(req("/", { method: "HEAD", headers: PAGE }), ENV, originReturning(503));
  assert.equal(res.status, 503);
  assert.equal(await res.text(), "");
});

test("healthy origin → response passed through untouched", async () => {
  const res = await handle(req("/ko/chat", { headers: PAGE }), ENV, originReturning(200, "<html>ok</html>", { "x-origin": "1" }));
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("x-origin"), "1");
  assert.equal(await res.text(), "<html>ok</html>");
});

test("API 5xx is NOT replaced with HTML (frontend expects JSON)", async () => {
  const body = JSON.stringify({ detail: "지금 처리 중인 질문이 많습니다." });
  const res = await handle(
    req("/api/chat/stream?session_id=x&question=q", { headers: { accept: "text/event-stream" } }),
    ENV,
    originReturning(503, body, { "Content-Type": "application/json", "Retry-After": "10" }),
  );
  assert.equal(res.status, 503);
  assert.equal(res.headers.get("Retry-After"), "10");
  assert.equal(await res.text(), body);
});

test("API path is passed through even when the client sends Accept: text/html", async () => {
  const res = await handle(req("/api/health", { headers: PAGE }), ENV, originReturning(502, "bad gateway"));
  assert.equal(res.status, 502);
  assert.equal(await res.text(), "bad gateway");
});

test("API unreachable → plain 502 JSON, not HTML", async () => {
  const res = await handle(req("/api/session", { method: "POST", body: "{}" }), ENV, originThrowing);
  assert.equal(res.status, 502);
  assert.match(res.headers.get("Content-Type"), /application\/json/);
  assert.deepEqual(await res.json(), { detail: "origin unreachable" });
});

test("SSE stream body is returned as the origin's own stream object (no buffering)", async () => {
  const stream = new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode("event: token\r\ndata: {}\r\n\r\n")); c.close(); } });
  const origin = async () => new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  const res = await handle(req("/api/chat/stream?q=1", { headers: { accept: "text/event-stream" } }), ENV, origin);
  assert.equal(res.body, stream);
});

test("Next.js internals and static assets are never replaced", async () => {
  for (const path of ["/_next/static/chunks/main.js", "/favicon.ico", "/robots.txt"]) {
    const res = await handle(req(path, { headers: { accept: "*/*" } }), ENV, originReturning(503, "x"));
    assert.equal(res.status, 503, path);
    assert.equal(await res.text(), "x", path);
  }
});

test("maintenance flag in KV → maintenance page for pages, pass-through for API", async () => {
  const env = { ...ENV, OUTAGE: kv("on") };
  const page = await handle(req("/", { headers: PAGE }), env, originReturning(200, "fine"));
  assert.equal(page.status, 503);
  assert.equal(page.headers.get("X-CamChat-Outage"), "maintenance");
  assert.match(await page.text(), /예정된 점검/);
  const api = await handle(req("/api/health", { headers: { accept: "*/*" } }), env, originReturning(200, "{\"status\":\"ok\"}"));
  assert.equal(api.status, 200);
});

test("maintenance flag off / missing binding / KV failure → normal pass-through", async () => {
  for (const env of [{ ...ENV, OUTAGE: kv("off") }, { ...ENV }, { ...ENV, OUTAGE: { get: async () => { throw new Error("kv down"); } } }]) {
    _resetMaintenanceMemo();
    const res = await handle(req("/", { headers: PAGE }), env, originReturning(200, "fine"));
    assert.equal(res.status, 200);
  }
});

test("maintenance flag is read from KV at most once per 30 s within an isolate", async () => {
  let reads = 0;
  const env = { ...ENV, OUTAGE: { get: async () => { reads += 1; return "on"; } } };
  for (let i = 0; i < 5; i += 1) await handle(req("/", { headers: PAGE }), env, originReturning(200));
  assert.equal(reads, 1);
});

test("outage page HTML is built once and reused", async () => {
  const a = await (await handle(req("/", { headers: PAGE }), ENV, originReturning(503))).text();
  const b = await (await handle(req("/", { headers: PAGE }), ENV, originReturning(522))).text();
  assert.equal(a, b);
});

test("isPageRequest: sec-fetch-dest wins, then Accept; non-GET never", () => {
  assert.equal(isPageRequest(req("/", { headers: { "sec-fetch-dest": "document", accept: "*/*" } })), true);
  assert.equal(isPageRequest(req("/", { headers: { "sec-fetch-dest": "iframe", accept: "text/html" } })), true);
  assert.equal(isPageRequest(req("/", { headers: { "sec-fetch-dest": "empty", accept: "text/html" } })), false);
  assert.equal(isPageRequest(req("/", { headers: { accept: "text/html" } })), true);
  assert.equal(isPageRequest(req("/", { headers: { accept: "application/json" } })), false);
  assert.equal(isPageRequest(req("/", { method: "POST", headers: PAGE })), false);
});

test("toOrigin: production route is a no-op; workers.dev preview is rewritten to the origin host", () => {
  const prod = req("/ko/chat");
  assert.equal(toOrigin(prod, ENV), prod);
  const preview = new Request("https://camchat-outage.example.workers.dev/ko/chat?x=1", { headers: PAGE });
  const rewritten = toOrigin(preview, ENV);
  assert.equal(rewritten.url, "https://maruvis.kr/ko/chat?x=1");
  assert.equal(rewritten.headers.get("sec-fetch-dest"), "document");
});

test("RETRY_AFTER_S is honoured and falls back to 300 when malformed", async () => {
  const a = await handle(req("/", { headers: PAGE }), { ...ENV, RETRY_AFTER_S: "60" }, originReturning(503));
  assert.equal(a.headers.get("Retry-After"), "60");
  const b = await handle(req("/", { headers: PAGE }), { ...ENV, RETRY_AFTER_S: "soon" }, originReturning(503));
  assert.equal(b.headers.get("Retry-After"), "300");
});
