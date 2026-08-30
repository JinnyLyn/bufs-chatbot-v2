"use client";
import { useState, useCallback, useRef, useEffect } from "react";
import { sseUrl } from "@/lib/api";
import type { ChatMessage, StreamDoneData, SourceURL, SearchResultItem } from "@/lib/types";

export function useChat(sessionId: string | null) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamText, setStreamText] = useState("");
  const esRef = useRef<EventSource | null>(null);

  const sendMessage = useCallback(
    (question: string) => {
      if (!sessionId || !question.trim() || isStreaming) return;

      // Add user message
      const userMsg: ChatMessage = { role: "user", content: question };
      setMessages((prev) => [...prev, userMsg]);
      setIsStreaming(true);
      setStreamText("");

      let accumulated = "";

      const params: Record<string, string> = {
        session_id: sessionId,
        question: question.trim(),
      };
      const url = sseUrl("/api/chat/stream", params);

      const es = new EventSource(url);
      esRef.current = es;

      es.addEventListener("token", (e: MessageEvent) => {
        try {
          const { token } = JSON.parse(e.data);
          accumulated += token;
          setStreamText(accumulated);
        } catch { /* ignore parse errors */ }
      });

      es.addEventListener("clear", () => {
        accumulated = "";
        setStreamText("");
      });

      es.addEventListener("done", (e: MessageEvent) => {
        try {
          const data: StreamDoneData = JSON.parse(e.data);
          const assistantMsg: ChatMessage = {
            role: "assistant",
            content: data.answer,
            sourceUrls: data.source_urls,
            results: data.results,
            intent: data.intent,
            durationMs: data.duration_ms,
            rated: false,
          };
          setMessages((prev) => [...prev, assistantMsg]);
        } catch {
          setMessages((prev) => [
            ...prev,
            { role: "assistant", content: accumulated || "응답을 받지 못했습니다." },
          ]);
        }
        setIsStreaming(false);
        setStreamText("");
        es.close();
        esRef.current = null;
      });

      es.addEventListener("error", (e: MessageEvent) => {
        let errMsg = "오류가 발생했습니다. 다시 시도해 주세요.";
        try {
          const d = JSON.parse((e as MessageEvent).data);
          if (d.message) errMsg = d.message;
        } catch { /* use default */ }
        setMessages((prev) => [...prev, { role: "assistant", content: errMsg }]);
        setIsStreaming(false);
        setStreamText("");
        es.close();
        esRef.current = null;
      });

      es.onerror = () => {
        // Close unconditionally, and close THIS object rather than whatever esRef
        // currently points at. EventSource reconnects automatically, so skipping the
        // close (as the previous `if (esRef.current)` guard did once another handler
        // had nulled the ref) left a connection retrying forever — and every retry
        // starts a fresh LLM generation on the server.
        es.close();
        if (esRef.current !== es) return;  // a newer stream owns the UI state now
        if (accumulated) {
          setMessages((prev) => [...prev, { role: "assistant", content: accumulated }]);
        }
        setIsStreaming(false);
        setStreamText("");
        esRef.current = null;
      };
    },
    [sessionId, isStreaming]
  );

  // Unmount, client-side route change and tab close are not terminal SSE events, so
  // without this the connection stays open and the server keeps generating for a reader
  // that no longer exists — each abandoned stream pinning a slot on the shared GPU.
  useEffect(
    () => () => {
      esRef.current?.close();
      esRef.current = null;
    },
    [],
  );

  const clearMessages = useCallback(() => {
    setMessages([]);
    setStreamText("");
  }, []);

  return { messages, isStreaming, streamText, sendMessage, clearMessages, setMessages };
}
