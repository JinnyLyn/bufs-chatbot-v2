"use client";
import { useState, use } from "react";
import { useRouter } from "next/navigation";
import { MessageSquare, LogIn } from "lucide-react";
import type { Lang } from "@/lib/types";
import { t } from "@/lib/i18n";
import { useAuth } from "@/hooks/useAuth";

export default function LoginPage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang: rawLang } = use(params);
  const lang = (rawLang === "en" ? "en" : "ko") as Lang;
  const router = useRouter();
  const { login } = useAuth();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    const result = await login(username, password);
    setLoading(false);

    if (result.ok) {
      router.push(`/${lang}/chat`);
    } else {
      setError(result.error || t(lang, "auth.error_invalid"));
    }
  };

  return (
    <div className="min-h-dvh bg-slate-50 flex items-center justify-center px-4 py-8">
      <div className="w-full max-w-md">
        <div className="text-center mb-8">
          <div className="w-14 h-14 bg-blue-600 rounded-2xl flex items-center justify-center mx-auto mb-4 shadow-lg shadow-blue-200">
            <MessageSquare className="w-8 h-8 text-white" />
          </div>
          <h1 className="text-2xl font-black text-slate-900 tracking-tight">
            {t(lang, "brand.name")}
          </h1>
          <p className="text-sm text-slate-500 mt-1">{t(lang, "auth.login")}</p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="bg-white rounded-2xl border border-slate-200 p-6 shadow-sm space-y-4"
        >
          <div>
            <label htmlFor="username" className="block text-sm font-semibold text-slate-700 mb-1">
              {t(lang, "auth.username")}
            </label>
            <input
              id="username"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="username"
              autoComplete="username"
              required
              className="w-full px-4 py-3 border border-slate-200 rounded-xl text-sm focus:border-blue-400 focus:ring-4 focus:ring-blue-100 outline-none transition-all"
            />
          </div>

          <div>
            <label htmlFor="password" className="block text-sm font-semibold text-slate-700 mb-1">
              {t(lang, "auth.password")}
            </label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="********"
              autoComplete="current-password"
              required
              className="w-full px-4 py-3 border border-slate-200 rounded-xl text-sm focus:border-blue-400 focus:ring-4 focus:ring-blue-100 outline-none transition-all"
            />
          </div>

          {error && (
            <p
              role="alert"
              className="text-sm text-red-500 font-semibold bg-red-50 px-3 py-2 rounded-lg"
            >
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading || !username || !password}
            className="w-full py-3 bg-blue-600 text-white font-bold rounded-xl hover:bg-blue-700 disabled:bg-slate-300 transition-all shadow-lg shadow-blue-200 active:scale-[0.98]"
          >
            {loading ? (
              <span className="flex items-center justify-center gap-2">
                <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                ...
              </span>
            ) : (
              <span className="flex items-center justify-center gap-2">
                <LogIn className="w-4 h-4" />
                {t(lang, "auth.login_btn")}
              </span>
            )}
          </button>

          <p className="text-center text-sm text-slate-500">
            {t(lang, "auth.no_account")}{" "}
            <a href={`/${lang}/register`} className="text-blue-600 font-semibold hover:underline">
              {t(lang, "auth.register")}
            </a>
          </p>
        </form>

        {/* 로그인은 선택 — 계정 없이도 채팅은 쓸 수 있다는 걸 명시한다. */}
        <p className="text-center text-sm text-slate-400 mt-5">
          <a href={`/${lang}/chat`} className="hover:text-slate-600 hover:underline">
            {lang === "ko" ? "로그인 없이 둘러보기" : "Continue without signing in"}
          </a>
        </p>
      </div>
    </div>
  );
}
