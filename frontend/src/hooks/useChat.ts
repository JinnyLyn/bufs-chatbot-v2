"use client";
import { useState, useCallback, useRef } from "react";
import { sseUrl } from "@/lib/api";
import { getAuthToken } from "@/hooks/useAuth";
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
      // 로그인 상태면 토큰을 실어 이 턴을 계정 이력에 남긴다. EventSource는 헤더를
      // 못 붙이므로 쿼리로 보낸다 — 비로그인이면 그냥 빠지고 채팅은 그대로 동작한다.
      const token = getAuthToken();
      if (token) params.access_token = token;
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
        if (esRef.current) {
          // If we have accumulated text, use it as the answer
          if (accumulated) {
            setMessages((prev) => [...prev, { role: "assistant", content: accumulated }]);
          }
          setIsStreaming(false);
          setStreamText("");
          es.close();
          esRef.current = null;
        }
      };
    },
    [sessionId, isStreaming]
  );

  const clearMessages = useCallback(() => {
    setMessages([]);
    setStreamText("");
  }, []);

  return { messages, isStreaming, streamText, sendMessage, clearMessages, setMessages };
}
