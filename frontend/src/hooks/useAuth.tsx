"use client";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { ApiError, apiFetch } from "@/lib/api";
import type { AuthUser, Lang } from "@/lib/types";

const TOKEN_KEY = "camchat_auth_token";

/** 외부 모듈(EventSource 등 훅 밖)에서 현재 토큰을 읽기 위한 헬퍼. SSR 안전. */
export function getAuthToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

// 토큰만 저장한다. 사용자 정보를 localStorage에 캐시해 첫 렌더에 쓰면 서버 HTML과
// 어긋나 hydration 불일치가 난다 — 대신 /me 확인이 끝날 때까지 loading으로 가린다.
function clearStored() {
  localStorage.removeItem(TOKEN_KEY);
}

export interface AuthResult {
  ok: boolean;
  error?: string;
}

export interface RegisterInput {
  username: string;
  nickname: string;
  password: string;
  student_id: string;
  department: string;
  student_type: string;
}

interface AuthContextValue {
  user: AuthUser | null;
  isLoggedIn: boolean;
  loading: boolean;
  login: (username: string, password: string) => Promise<AuthResult>;
  register: (body: RegisterInput) => Promise<AuthResult>;
  logout: (opts?: { sessionId?: string | null }) => Promise<void>;
  authFetch: <T>(path: string, opts?: RequestInit) => Promise<T>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * 로그인 상태를 트리 전체가 공유하도록 Context로 제공한다.
 *
 * 훅 하나로 두면 컴포넌트마다 독립된 state가 생겨서, 사이드바에서 로그아웃해도
 * 헤더는 여전히 로그인 상태로 보이는 어긋남이 생긴다. 토큰 자체는 localStorage에
 * 두고(새로고침 유지), React state는 여기 한 곳에서만 관리한다.
 */
export function AuthProvider({ lang, children }: { lang: Lang; children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

  // 마운트 시 저장된 토큰을 서버에 확인한다. 캐시된 사용자를 먼저 보여줘 깜빡임을 막고,
  // 검증에 실패하면(만료·서명키 변경) 조용히 로그아웃 상태로 되돌린다.
  useEffect(() => {
    let cancelled = false;

    // 토큰이 없으면 확인할 것도 없지만, 상태 갱신은 모두 이 프라미스 이후에 일어나야
    // 한다 — 이펙트 본문에서 곧바로 setState 하면 렌더가 연쇄로 다시 돈다.
    const validated: Promise<AuthUser | null> = (() => {
      const token = getAuthToken();
      if (!token) return Promise.resolve(null);
      return apiFetch<AuthUser>(`/api/user/me?lang=${lang}`, {
        headers: { Authorization: `Bearer ${token}` },
      }).catch(() => null);
    })();

    void validated.then((data) => {
      if (cancelled) return;
      if (data) {
        setUser(data);
      } else {
        // 토큰이 없거나 만료·서명키 변경으로 무효 — 조용히 로그아웃 상태로.
        clearStored();
        setUser(null);
      }
      setLoading(false);
    });

    return () => {
      cancelled = true;
    };
  }, [lang]);

  const persist = useCallback((token: string, nextUser: AuthUser) => {
    localStorage.setItem(TOKEN_KEY, token);
    setUser(nextUser);
  }, []);

  const login = useCallback(
    async (username: string, password: string): Promise<AuthResult> => {
      try {
        const data = await apiFetch<{ token: string; user: AuthUser }>(
          `/api/user/login?lang=${lang}`,
          { method: "POST", body: JSON.stringify({ username, password }) },
        );
        persist(data.token, data.user);
        return { ok: true };
      } catch (e) {
        return { ok: false, error: e instanceof Error ? e.message : String(e) };
      }
    },
    [lang, persist],
  );

  const register = useCallback(
    async (body: RegisterInput): Promise<AuthResult> => {
      try {
        const data = await apiFetch<{ token: string; user: AuthUser }>(
          `/api/user/register?lang=${lang}`,
          { method: "POST", body: JSON.stringify(body) },
        );
        persist(data.token, data.user);
        return { ok: true };
      } catch (e) {
        return { ok: false, error: e instanceof Error ? e.message : String(e) };
      }
    },
    [lang, persist],
  );

  const logout = useCallback(async (opts?: { sessionId?: string | null }) => {
    const token = getAuthToken();
    const sid = opts?.sessionId;
    // 토큰이 없어도 session_id가 있으면 서버 세션은 정리해야 한다.
    try {
      const qs = sid ? `?session_id=${encodeURIComponent(sid)}` : "";
      await apiFetch(`/api/user/logout${qs}`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      });
    } catch {
      // 서버가 실패해도 로컬 세션은 반드시 끊는다.
    }
    clearStored();
    setUser(null);
  }, []);

  const authFetch = useCallback(async <T,>(path: string, opts?: RequestInit): Promise<T> => {
    const token = getAuthToken();
    if (!token) throw new ApiError(401, "로그인이 필요합니다.");
    try {
      return await apiFetch<T>(path, {
        ...opts,
        headers: { Authorization: `Bearer ${token}`, ...(opts?.headers ?? {}) },
      });
    } catch (e) {
      // 토큰이 만료·폐기된 상태 — 화면을 로그아웃으로 되돌린다.
      if (e instanceof ApiError && e.status === 401) {
        clearStored();
        setUser(null);
      }
      throw e;
    }
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ user, isLoggedIn: !!user, loading, login, register, logout, authFetch }),
    [user, loading, login, register, logout, authFetch],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>");
  return ctx;
}
