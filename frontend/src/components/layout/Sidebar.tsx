"use client";
import { useEffect } from "react";
import { MessageSquare, PlusCircle, History, X, LogIn, LogOut, User } from "lucide-react";
import { useRouter } from "next/navigation";
import type { Lang, ChatMessage } from "@/lib/types";
import { t } from "@/lib/i18n";
import { useAuth } from "@/hooks/useAuth";
import { useChatHistory } from "@/hooks/useChatHistory";

interface SidebarProps {
  lang: Lang;
  messages?: ChatMessage[]; // current-session messages → conversation history
  sessionId?: string | null; // 로그아웃 시 서버 세션까지 정리하기 위해 전달
  onSelectQuestion: (q: string) => void;
  onClearChat: () => void;
  onNewChat: () => void;
  isOpen: boolean;
  onClose: () => void;
}

export default function Sidebar({
  lang,
  messages = [],
  sessionId,
  onSelectQuestion,
  onClearChat,
  onNewChat,
  isOpen,
  onClose,
}: SidebarProps) {
  const router = useRouter();
  const { user, isLoggedIn, loading: authLoading, logout } = useAuth();
  const { items: savedHistory, refresh: refreshHistory } = useChatHistory({ limit: 30 });

  // 답변이 하나 끝날 때마다 계정 이력을 다시 읽는다 (로그인 상태에서만 의미 있음).
  const turnCount = messages.length;
  useEffect(() => {
    if (isLoggedIn) void refreshHistory();
  }, [turnCount, isLoggedIn, refreshHistory]);

  // 로그인 상태면 계정에 쌓인 이력을, 아니면 이번 세션의 질문만 보여준다.
  const sessionHistory = messages
    .filter((m) => m.role === "user")
    .map((m, i) => ({ id: `s-${i}`, question: m.content }))
    .reverse()
    .slice(0, 30);
  const historyEntries = isLoggedIn
    ? savedHistory.map((h) => ({ id: `db-${h.id}`, question: h.question }))
    : sessionHistory;

  const emptyHistoryText = isLoggedIn
    ? lang === "ko"
      ? "아직 저장된 질문이 없습니다."
      : "No saved questions yet."
    : lang === "ko"
      ? "이번 대화 기록이 여기 표시됩니다."
      : "Your conversation will appear here.";

  const handleLogout = async () => {
    await logout({ sessionId });
    onClose();
    // 로그아웃 후에는 새 세션으로 시작하도록 채팅 화면을 다시 로드한다.
    window.location.href = `/${lang}/chat`;
  };

  return (
    <>
      {/* Overlay — 모바일 전용. lg 이상에서는 사이드바가 기본으로 열려 있는데, 이 오버레이가
          화면 전체(z-30)를 덮어 헤더(z-10)의 로그인·언어 버튼까지 클릭을 가로챘다. */}
      {isOpen && <div className="fixed inset-0 bg-black/30 z-30 lg:hidden" onClick={onClose} />}

      <aside
        className={`fixed top-0 left-0 h-full w-72 bg-slate-50 border-r border-slate-200 z-40 flex flex-col transition-transform duration-300 ${
          isOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        {/* Brand */}
        <div className="p-5 flex items-center justify-between">
          <div className="flex items-center gap-3 cursor-pointer" onClick={onClose}>
            <div className="w-10 h-10 bg-blue-600 rounded-xl flex items-center justify-center shadow-lg shadow-blue-200">
              <MessageSquare className="w-6 h-6 text-white" />
            </div>
            <span className="font-bold text-xl tracking-tight">{t(lang, "brand.name")}</span>
          </div>
          <button onClick={onClose} className="lg:hidden p-1.5 hover:bg-slate-200 rounded-lg text-slate-400">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* New Chat */}
        <div className="px-4 mb-3">
          <button
            onClick={() => {
              onNewChat();
              onClose();
            }}
            className="w-full py-2.5 px-4 bg-white border border-slate-200 rounded-xl flex items-center gap-3 hover:border-blue-400 hover:text-blue-600 transition-all shadow-sm font-semibold text-sm"
          >
            <PlusCircle className="w-5 h-5" />
            {t(lang, "sidebar.new_chat")}
          </button>
        </div>

        {/* History */}
        <nav className="flex-grow px-4 space-y-0.5 overflow-y-auto">
          <p className="px-4 py-2 text-[10px] font-bold text-slate-400 uppercase tracking-widest">
            {t(lang, "sidebar.history")}
          </p>
          {historyEntries.length === 0 ? (
            <p className="px-4 py-3 text-xs text-slate-400">{emptyHistoryText}</p>
          ) : (
            historyEntries.map((h) => (
              <button
                key={h.id}
                onClick={() => {
                  onSelectQuestion(h.question);
                  onClose();
                }}
                title={h.question}
                className="w-full flex items-center gap-3 px-4 py-2 text-sm font-medium text-slate-500 hover:bg-white hover:text-blue-600 rounded-xl transition-all group text-left"
              >
                <History className="w-4 h-4 opacity-50 group-hover:opacity-100 shrink-0" />
                <span className="truncate">{h.question}</span>
              </button>
            ))
          )}
        </nav>

        {/* Bottom */}
        <div className="p-4 border-t border-slate-200 space-y-1">
          {/* 토큰 검증 중에는 자리만 잡아둔다 — 로그인 버튼이 번쩍였다 사라지지 않도록. */}
          {authLoading ? (
            <div className="h-10" aria-hidden />
          ) : isLoggedIn && user ? (
            <>
              <div className="flex items-center gap-3 px-3 py-2 mb-1">
                <div className="w-9 h-9 bg-blue-100 rounded-full flex items-center justify-center shrink-0">
                  <User className="w-5 h-5 text-blue-600" />
                </div>
                <div className="min-w-0">
                  <p className="text-sm font-bold text-slate-700 truncate">{user.nickname}</p>
                  <p className="text-[11px] text-slate-400 truncate">
                    {user.student_id}
                    {t(lang, "sidebar.year_suffix")} · {user.department}
                  </p>
                </div>
              </div>
              <button
                onClick={handleLogout}
                className="w-full flex items-center gap-3 px-4 py-2 text-sm font-medium text-slate-500 hover:bg-slate-100 rounded-xl transition-all"
              >
                <LogOut className="w-5 h-5 opacity-70" />
                {t(lang, "sidebar.logout")}
              </button>
            </>
          ) : (
            <button
              onClick={() => {
                onClose();
                router.push(`/${lang}/login`);
              }}
              className="w-full flex items-center gap-3 px-4 py-2 text-sm font-semibold text-blue-600 hover:bg-blue-50 rounded-xl transition-all"
            >
              <LogIn className="w-5 h-5" />
              {t(lang, "auth.login")}
            </button>
          )}

          <button
            onClick={() => {
              onClearChat();
              onClose();
            }}
            className="w-full flex items-center gap-3 px-4 py-2 text-sm font-medium text-slate-500 hover:bg-slate-100 rounded-xl transition-all"
          >
            <X className="w-5 h-5 opacity-70" />
            {t(lang, "sidebar.clear_chat")}
          </button>
          <p className="text-center text-[10px] text-slate-400 pt-1">Agentic RAG · v0.1.0</p>
        </div>
      </aside>
    </>
  );
}
