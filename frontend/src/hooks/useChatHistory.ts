"use client";
import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/hooks/useAuth";
import type { ChatHistoryItem, ChatHistoryResponse } from "@/lib/types";

/**
 * 로그인 사용자의 질문 이력 (GET /api/user/chat-history).
 *
 * 비로그인 상태에서는 아무것도 조회하지 않고 빈 목록을 돌려준다 — 사이드바가
 * 로그인 여부와 무관하게 같은 훅을 쓸 수 있도록.
 */
export function useChatHistory(opts?: { limit?: number }) {
  const { isLoggedIn, authFetch } = useAuth();
  const [items, setItems] = useState<ChatHistoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);

  const limit = opts?.limit ?? 30;

  const refresh = useCallback(async () => {
    if (!isLoggedIn) {
      setItems([]);
      setTotal(0);
      return;
    }
    setLoading(true);
    try {
      const r = await authFetch<ChatHistoryResponse>(
        `/api/user/chat-history?limit=${limit}&offset=0`,
      );
      setItems(r.items);
      setTotal(r.total);
    } catch {
      // 401·네트워크 오류 — 이력은 부가 기능이므로 조용히 빈 목록으로 둔다.
      setItems([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [isLoggedIn, authFetch, limit]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { items, total, loading, refresh, isLoggedIn };
}
