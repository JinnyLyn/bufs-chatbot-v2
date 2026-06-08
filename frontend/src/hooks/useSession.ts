"use client";
import { useState, useEffect, useCallback } from "react";
import { apiFetch } from "@/lib/api";
import type { Lang, SessionInfo, UserProfile } from "@/lib/types";

// 2026-04-28: 세션을 매 페이지 로드마다 새로 생성 (새로고침/새 창 = 새 대화).
// 이전엔 쿠키(camchat_session_id, expires=+1일)에 sid를 저장해서 새로고침해도
// 같은 sid 사용 → 이전 대화 history가 multi-turn rewrite와 LLM history injection을
// 통해 다음 질문에 영향을 줌. 사용자 mental model("새로고침 = 새 대화")과 어긋나
// 쿠키 제거. 같은 탭이 살아있는 동안에는 React state로 sid 유지(멀티턴 가능).
const _LEGACY_COOKIE_KEY = "camchat_session_id";

function _clearLegacyCookie() {
  if (typeof document === "undefined") return;
  document.cookie = `${_LEGACY_COOKIE_KEY}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT; SameSite=Lax`;
}

export function useSession(lang: Lang) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [loading, setLoading] = useState(true);

  const createSession = useCallback(async () => {
    const data = await apiFetch<SessionInfo>("/api/session", {
      method: "POST",
      body: JSON.stringify({ lang }),
    });
    setSessionId(data.session_id);
    setSession(data);
    return data.session_id;
  }, [lang]);

  useEffect(() => {
    let cancelled = false;

    async function loadSession() {
      // 페이지 로드마다 항상 새 session 생성 — 새로고침/새 창 = 새 대화 (이전 history 무관).
      _clearLegacyCookie();
      try {
        await createSession();
      } catch {
        // ignore — chat 첫 호출 시 다시 시도됨
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void loadSession();

    return () => {
      cancelled = true;
    };
  }, [createSession]);

  const updateProfile = useCallback(async (profile: UserProfile) => {
    if (!sessionId) return;
    await apiFetch(`/api/session/${sessionId}/profile`, {
      method: "PUT",
      body: JSON.stringify(profile),
    });
    setSession((prev) => prev ? { ...prev, user_profile: profile } : prev);
  }, [sessionId]);

  // 서버에서 최신 세션 상태를 재조회 (업로드 후 has_transcript 갱신용)
  const refreshSession = useCallback(async () => {
    if (!sessionId) return;
    try {
      const data = await apiFetch<SessionInfo>(`/api/session/${sessionId}`);
      setSession({ ...data, lang });
    } catch {
      // ignore — 세션 무효면 loadSession이 재생성
    }
  }, [sessionId, lang]);

  // 세션 완전 리셋 — 로그아웃용. 잔여 쿠키 삭제 + 상태 클리어 + 새 빈 세션 생성.
  const resetSession = useCallback(async () => {
    _clearLegacyCookie();
    setSessionId(null);
    setSession(null);
    await createSession();
  }, [createSession]);

  return { sessionId, session, loading, updateProfile, createSession, refreshSession, resetSession };
}
