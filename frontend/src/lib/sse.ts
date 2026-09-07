// Minimal Server-Sent Events parser for `fetch` + ReadableStream.
//
// EventSource cannot see the HTTP status or headers, so it cannot tell a saturated
// server (503 + Retry-After) from a dead one — and it reconnects on its own, starting a
// fresh generation each time. Reading the stream through fetch gives the chat hook
// both the status and full control over retries; this parser turns the raw bytes into
// the same `event`/`data` pairs EventSource used to deliver.
//
// Wire format (https://html.spec.whatwg.org/multipage/server-sent-events.html):
//   - events are separated by a blank line; lines end with \n, \r\n or \r
//   - `field: value` lines; only `event` and `data` matter here
//   - multiple `data:` lines join with \n
//   - lines starting with ":" are comments (sse-starlette sends `: ping` keepalives)

export interface SseEvent {
  event: string;
  data: string;
}

export interface SseParser {
  /** Feed a decoded chunk; dispatches every complete event it contains. */
  feed(chunk: string): void;
  /** Dispatch a trailing event that was not followed by a blank line. */
  flush(): void;
}

export function createSseParser(onEvent: (e: SseEvent) => void): SseParser {
  let buffer = "";
  let eventName = "";
  let dataLines: string[] = [];

  const dispatch = () => {
    if (dataLines.length > 0) {
      onEvent({ event: eventName || "message", data: dataLines.join("\n") });
    }
    eventName = "";
    dataLines = [];
  };

  const handleLine = (line: string) => {
    if (line === "") {
      dispatch();
      return;
    }
    if (line.startsWith(":")) return; // comment / keepalive
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") eventName = value;
    else if (field === "data") dataLines.push(value);
    // `id` and `retry` are irrelevant for a one-shot stream.
  };

  return {
    feed(chunk: string) {
      buffer += chunk;
      // Keep a trailing "\r" in the buffer: it may be the first half of "\r\n".
      let searchFrom = 0;
      for (;;) {
        const nl = buffer.indexOf("\n", searchFrom);
        const cr = buffer.indexOf("\r", searchFrom);
        let end: number;
        let skip: number;
        if (nl === -1 && cr === -1) break;
        if (cr !== -1 && (nl === -1 || cr < nl)) {
          if (cr === buffer.length - 1) break; // lone trailing \r — wait for more
          end = cr;
          skip = buffer[cr + 1] === "\n" ? 2 : 1;
        } else {
          end = nl;
          skip = 1;
        }
        handleLine(buffer.slice(searchFrom, end));
        searchFrom = end + skip;
      }
      buffer = buffer.slice(searchFrom);
    },
    flush() {
      if (buffer.length > 0) {
        handleLine(buffer.replace(/\r?\n?$/, ""));
        buffer = "";
      }
      dispatch();
    },
  };
}
