// Run: npm test (node --test with type stripping; no test framework needed).
//
// These three cases are the ones the outage logic depends on: a CRLF pair split across
// chunks must not produce a phantom event, a keepalive comment must dispatch NOTHING
// (that is what keeps `: ping` from counting as progress), and a trailing event without
// a terminating blank line must still come out of `flush()`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createSseParser, type SseEvent } from "./sse.ts";

function collect() {
  const events: SseEvent[] = [];
  const parser = createSseParser((e) => events.push(e));
  return { events, parser };
}

test("parses event/data pairs framed with CRLF, as sse-starlette sends them", () => {
  const { events, parser } = collect();
  parser.feed('event: token\r\ndata: {"token": "a"}\r\n\r\nevent: status\r\ndata: {"stage": "writing"}\r\n\r\n');
  assert.deepEqual(events, [
    { event: "token", data: '{"token": "a"}' },
    { event: "status", data: '{"stage": "writing"}' },
  ]);
});

test("a CRLF split across two chunks yields exactly one event", () => {
  const { events, parser } = collect();
  parser.feed("event: token\r\ndata: x\r");
  assert.equal(events.length, 0);
  parser.feed("\n\r\n");
  assert.deepEqual(events, [{ event: "token", data: "x" }]);
});

test("a chunk boundary inside a line does not split the line", () => {
  const { events, parser } = collect();
  parser.feed("event: to");
  parser.feed("ken\ndata: {\"tok");
  parser.feed("en\": \"b\"}\n\n");
  assert.deepEqual(events, [{ event: "token", data: '{"token": "b"}' }]);
});

test("keepalive comments dispatch nothing", () => {
  const { events, parser } = collect();
  parser.feed(": ping - 1788679358\r\n\r\n: ping - 1788679373\r\n\r\n");
  parser.flush();
  assert.equal(events.length, 0);
});

test("multiple data lines join with newline; missing event name defaults to message", () => {
  const { events, parser } = collect();
  parser.feed("data: first\ndata: second\n\n");
  assert.deepEqual(events, [{ event: "message", data: "first\nsecond" }]);
});

test("flush() dispatches a trailing event that had no blank line", () => {
  const { events, parser } = collect();
  parser.feed("event: done\ndata: {}");
  assert.equal(events.length, 0);
  parser.flush();
  assert.deepEqual(events, [{ event: "done", data: "{}" }]);
});

test("an event with no data lines is ignored", () => {
  const { events, parser } = collect();
  parser.feed("event: clear\n\nevent: token\ndata: y\n\n");
  assert.deepEqual(events, [{ event: "token", data: "y" }]);
});

test("a lone CR line ending is accepted (the final CR waits for the next byte or flush)", () => {
  const { events, parser } = collect();
  parser.feed("event: token\rdata: z\r\r");
  // The trailing "\r" may be the first half of "\r\n", so the blank line is held back.
  assert.equal(events.length, 0);
  parser.feed("event: token\rdata: w\r\r\n");
  assert.deepEqual(events, [{ event: "token", data: "z" }, { event: "token", data: "w" }]);
  parser.flush();
  assert.equal(events.length, 2);
});
