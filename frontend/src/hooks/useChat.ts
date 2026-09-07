"use client";
import { useState, useCallback, useRef, useEffect } from "react";
import { sseUrl } from "@/lib/api";
import { createSseParser } from "@/lib/sse";
import { t } from "@/lib/i18n";
import {
  STALL_SOFT_MS, STALL_HARD_MS, BUSY_RETRY_AFTER_S, RETRY_AFTER_MAX_S,
} from "@/lib/constants";
import type {
  ChatMessage, ChatErrorKind, Lang, StreamDoneData, StreamProgress,
} from "@/lib/types";

// 장애 대응 보고서(reports/CamChat-장애대응.pdf §7)의 학생 화면 기준을 구현한다.
//
// - EventSource 대신 fetch + ReadableStream: 503/429 의 상태·Retry-After 를 읽을 수 있고,
//   끊긴 연결을 브라우저가 몰래 재접속해 서버에 새 생성을 또 시키는 일이 없다.
// - 무진행 45초: 안내만 하고 요청은 계속. 무진행 120초: 중단하고 [다시 시도].
//   "무진행"은 마지막 status/token 이후의 시간이다(keepalive ping 은 진행이 아니다).
// - 연결 종료·서버 오류: 받은 부분 답변이 있으면 남기고, 그 아래 안내 + [다시 시도].
// - 혼잡(503 + Retry-After, 또는 스트림 안의 busy 오류): 다시 시도와 새 질문을 그 시간 동안 잠근다.

interface Failure {
  kind: ChatErrorKind;
  /** 혼잡 응답이 요구한 대기 시간(초). */
  retryAfterS?: number;
  /** 서버가 보낸 안내 문구. 없으면 i18n 확정 문구를 쓴다. */
  message?: string;
}

interface Run {
  controller: AbortController;
  /** abort 를 누가 했는지. null 이면 네트워크 쪽에서 끊긴 것이다. */
  reason: "timeout" | "user" | null;
}

function parseRetryAfter(raw: string | null): number | null {
  const s = raw?.trim();
  if (!s) return null;
  const secs = Number(s);
  if (Number.isFinite(secs) && secs >= 0) return Math.ceil(secs);
  const at = Date.parse(s);
  if (Number.isNaN(at)) return null;
  return Math.max(0, Math.ceil((at - Date.now()) / 1000));
}

export function useChat(
  lang: Lang,
  sessionId: string | null,
  /**
   * 세션이 아직 없을 때(페이지 로드 시 백엔드가 죽어 있었을 때) 다시 만든다.
   * `signal` 을 실제 요청까지 넘겨야 한다 — 기다리기만 포기하면 타임아웃 뒤에 늦게 온 응답이
   * 재시도로 만든 최신 세션을 낡은 세션으로 덮어쓴다.
   */
  ensureSession: (signal?: AbortSignal) => Promise<string>,
) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamText, setStreamText] = useState("");
  const [progress, setProgress] = useState<StreamProgress | null>(null);
  const [stalled, setStalled] = useState(false);
  // 혼잡 잠금이 풀리는 시각(ms). null 이면 잠금 없음. 잠글 때와 풀릴 때 두 번만 바뀐다 —
  // 초 단위 카운트다운은 안내 말풍선이 스스로 센다(페이지 전체가 1초마다 다시 그려지지 않게).
  const [retryUntil, setRetryUntil] = useState<number | null>(null);

  const runRef = useRef<Run | null>(null);
  const lastQuestionRef = useRef<string | null>(null);
  // ref 는 sendMessage/retry 가 렌더 없이 즉시 확인하는 용도. 항상 setLock 으로만 같이 바꾼다.
  const retryUntilRef = useRef(0);
  const setLock = useCallback((until: number | null) => {
    retryUntilRef.current = until ?? 0;
    setRetryUntil(until);
  }, []);
  const locked = () => Date.now() < retryUntilRef.current;

  useEffect(() => {
    if (retryUntil === null) return;
    const id = setTimeout(() => setLock(null), Math.max(0, retryUntil - Date.now()));
    return () => clearTimeout(id);
  }, [retryUntil, setLock]);

  const start = useCallback(
    async (question: string, appendUser: boolean) => {
      const run: Run = { controller: new AbortController(), reason: null };
      runRef.current = run;
      const mine = () => runRef.current === run;

      if (appendUser) {
        setMessages((prev) => [...prev, { role: "user", content: question }]);
      }
      lastQuestionRef.current = question;
      setIsStreaming(true);
      setStreamText("");
      setProgress(null);
      setStalled(false);
      setLock(null);

      let accumulated = "";
      let softTimer: ReturnType<typeof setTimeout> | undefined;
      let hardTimer: ReturnType<typeof setTimeout> | undefined;
      const clearTimers = () => {
        clearTimeout(softTimer);
        clearTimeout(hardTimer);
      };
      // 진행 신호(status/token/clear)마다 다시 잰다. 총 처리시간이 아니라 무진행 시간이다.
      const armTimers = () => {
        clearTimers();
        softTimer = setTimeout(() => {
          if (mine()) setStalled(true);
        }, STALL_SOFT_MS);
        hardTimer = setTimeout(() => {
          run.reason = "timeout";
          run.controller.abort();
        }, STALL_HARD_MS);
      };
      const signal = () => {
        setStalled(false);
        armTimers();
      };

      const finish = () => {
        clearTimers();
        if (!mine()) return;
        setIsStreaming(false);
        setStreamText("");
        setProgress(null);
        setStalled(false);
        runRef.current = null;
      };

      const fail = (f: Failure) => {
        clearTimers();
        if (!mine()) return;
        const content =
          f.message || t(lang, f.kind === "busy" ? "chat.err.busy" : "chat.err.fetch");
        setMessages((prev) => {
          const next = [...prev];
          // 받은 부분 답변은 빈 화면 대신 그대로 남긴다(§7.3). 다시 시도가 성공하면 걷어낸다.
          if (accumulated) next.push({ role: "assistant", content: accumulated, partial: true });
          next.push({ role: "assistant", content, errorKind: f.kind });
          return next;
        });
        // 혼잡이면 서버가 정한 시간만큼, 120초 무진행 중단이면 기본 시간만큼 잠근다. 중단된
        // 생성은 서버에서 다음 이벤트까지 계속 돌며 슬롯을 쥐고 있어서, 곧바로 재전송하면 포화된
        // 순간에 같은 질문이 두 개가 된다.
        if (f.kind === "busy" || f.kind === "timeout") {
          const s = Number(f.retryAfterS);
          const wait = Number.isFinite(s) && s >= 0 ? s : BUSY_RETRY_AFTER_S;
          setLock(Date.now() + Math.min(Math.ceil(wait), RETRY_AFTER_MAX_S) * 1000);
        }
        finish();
      };
      const failAborted = () => {
        clearTimers();
        if (run.reason === "user") return; // 페이지 이탈: 안내할 대상이 없다
        fail({ kind: run.reason === "timeout" ? "timeout" : "disconnect" });
      };

      // 세션 생성도 무진행 타이머 안에서 한다. 백엔드가 죽어 있으면 페이지 로드 때의 세션 생성이
      // 실패해 여기서 다시 만드는데, 그 요청이 영영 답이 없으면 여기서 120초에 끊어야 한다.
      armTimers();
      let sid: string;
      try {
        sid = sessionId ?? (await ensureSession(run.controller.signal));
      } catch {
        if (run.controller.signal.aborted) failAborted();
        else fail({ kind: "disconnect" });
        return;
      }
      if (!mine()) {
        clearTimers();
        return;
      }
      armTimers(); // 세션 생성에 쓴 시간을 빼지 않고 질문 요청에 다시 45/120초를 준다

      let res: Response;
      try {
        res = await fetch(sseUrl("/api/chat/stream", { session_id: sid, question }), {
          signal: run.controller.signal,
          headers: { Accept: "text/event-stream" },
          cache: "no-store",
        });
      } catch {
        failAborted();
        return;
      }
      if (!mine()) {
        clearTimers();
        void res.body?.cancel().catch(() => {});
        return;
      }

      if (!res.ok) {
        const retryAfter = parseRetryAfter(res.headers.get("Retry-After"));
        let detail: string | undefined;
        try {
          const body: unknown = await res.json();
          const d = (body as { detail?: unknown })?.detail;
          if (typeof d === "string") detail = d;
          // FastAPI 422 는 detail 을 배열로 보낸다: [{ msg, loc, ... }]
          else if (Array.isArray(d) && typeof d[0]?.msg === "string") detail = d[0].msg;
        } catch {
          /* HTML error page (Cloudflare/tunnel) or empty body — use the fixed wording */
        }
        // 503/429 가 "혼잡"인지는 백엔드가 보낸 흔적(Retry-After 헤더 또는 JSON detail)으로
        // 가른다. Cloudflare·터널이 내는 5xx 는 HTML 이고 그 헤더도 없으므로 일반 오류 문구로
        // 떨어진다. 헤더만 프록시에서 떨어져 나간 경우엔 기본 대기 시간을 쓴다.
        const busy = (res.status === 503 || res.status === 429) && (retryAfter !== null || detail !== undefined);
        if (busy) {
          fail({ kind: "busy", retryAfterS: retryAfter ?? BUSY_RETRY_AFTER_S, message: detail });
        } else {
          // 4xx 의 detail 은 학생이 고칠 수 있는 이유다(질문이 너무 김 등) — 그대로 보여 준다.
          // 5xx 의 detail("Internal Server Error")은 도움이 안 되므로 확정 문구를 쓴다.
          fail({ kind: "server", message: res.status < 500 ? detail : undefined });
        }
        return;
      }
      if (!res.body) {
        fail({ kind: "server" });
        return;
      }

      let terminal = false; // done 또는 error 이벤트를 받았다
      const parser = createSseParser(({ event, data }) => {
        if (terminal) return;
        switch (event) {
          case "token":
            try {
              const { token } = JSON.parse(data) as { token: string };
              accumulated += token;
              setStreamText(accumulated);
            } catch { /* ignore parse errors */ }
            signal();
            break;
          case "clear":
            accumulated = "";
            setStreamText("");
            signal();
            break;
          case "status":
            try {
              const p = JSON.parse(data) as StreamProgress;
              if (p?.stage) setProgress(p);
            } catch { /* ignore parse errors */ }
            signal();
            break;
          case "done": {
            terminal = true;
            clearTimers();
            try {
              const d: StreamDoneData = JSON.parse(data);
              setMessages((prev) => [
                ...prev,
                {
                  role: "assistant",
                  content: d.answer,
                  sourceUrls: d.source_urls,
                  results: d.results,
                  intent: d.intent,
                  durationMs: d.duration_ms,
                  rated: false,
                },
              ]);
            } catch {
              setMessages((prev) => [
                ...prev,
                { role: "assistant", content: accumulated || t(lang, "chat.empty_response") },
              ]);
            }
            finish();
            break;
          }
          case "error": {
            terminal = true;
            let d: { message?: string; code?: string; retry_after?: number } = {};
            try {
              d = JSON.parse(data);
            } catch { /* use defaults */ }
            if (d.code === "busy") {
              fail({ kind: "busy", retryAfterS: d.retry_after, message: d.message });
            } else {
              fail({ kind: "server", message: d.message });
            }
            break;
          }
          default:
            break;
        }
      });

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      try {
        for (;;) {
          const { value, done: eof } = await reader.read();
          if (eof) break;
          parser.feed(decoder.decode(value, { stream: true }));
          if (terminal) break;
        }
        // 빈 줄 없이 끝난 잔여 이벤트는 버린다(SSE 규격). done 페이로드 중간에 끊긴 스트림을
        // flush 로 살려 내면 잘린 답변이 완성본으로 올라간다.
      } catch {
        if (!terminal) failAborted();
        return;
      }
      if (terminal) {
        void reader.cancel().catch(() => {});
      } else {
        // 서버가 done 없이 연결을 닫았다(프로세스 재시작, 터널 끊김 등).
        fail({ kind: "disconnect" });
      }
    },
    [lang, sessionId, ensureSession, setLock],
  );

  // 혼잡 잠금 동안은 새 질문도 막는다. 다시 시도 버튼만 잠그면 학생은 같은 질문을 다시 쳐서
  // 보내고, 그 요청이 정확히 서버가 포화된 순간에 도착한다(§7.4).
  const sendMessage = useCallback(
    (question: string) => {
      const q = question.trim();
      if (!q || runRef.current || locked()) return;
      void start(q, true);
    },
    [start],
  );

  // 마지막 질문을 그대로 다시 보낸다(§7.2). 사용자 말풍선은 이미 있으므로 다시 붙이지 않고,
  // 직전 안내 말풍선과 잘린 부분 답변만 걷어낸다.
  const retry = useCallback(() => {
    const q = lastQuestionRef.current;
    if (!q || runRef.current || locked()) return;
    setMessages((prev) => {
      let end = prev.length;
      while (end > 0 && (prev[end - 1].errorKind || prev[end - 1].partial)) end -= 1;
      return prev.slice(0, end);
    });
    void start(q, false);
  }, [start]);

  // 언마운트·라우트 이동·탭 종료는 SSE 종료 이벤트가 아니다. 끊지 않으면 서버는 읽는 사람이
  // 없는 답변을 계속 만들고, 버려진 스트림마다 공유 GPU 슬롯 하나가 묶인다.
  useEffect(
    () => () => {
      const run = runRef.current;
      if (!run) return;
      run.reason = "user";
      run.controller.abort();
      runRef.current = null;
    },
    [],
  );

  const last = messages[messages.length - 1];
  const canRetry = !isStreaming && !!last?.errorKind && lastQuestionRef.current !== null;

  return {
    messages, isStreaming, streamText, progress, stalled,
    canRetry, retryUntil, retry, sendMessage,
  };
}
