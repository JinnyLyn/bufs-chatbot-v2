"use client";
import { MessageSquare, PlusCircle, History, X } from "lucide-react";
import type { Lang, ChatMessage } from "@/lib/types";
import { t } from "@/lib/i18n";

interface SidebarProps {
  lang: Lang;
  messages?: ChatMessage[]; // current-session messages → conversation history
  onSelectQuestion: (q: string) => void;
  onClearChat: () => void;
  onNewChat: () => void;
  isOpen: boolean;
  onClose: () => void;
}

export default function Sidebar({
  lang,
  messages = [],
  onSelectQuestion,
  onClearChat,
  onNewChat,
  isOpen,
  onClose,
}: SidebarProps) {
  // This session's user questions, newest first.
  const sessionHistory = messages
    .filter((m) => m.role === "user")
    .map((m, i) => ({ id: `s-${i}`, question: m.content }))
    .reverse()
    .slice(0, 30);

  return (
    <>
      {/* Overlay */}
      {isOpen && <div className="fixed inset-0 bg-black/30 z-30" onClick={onClose} />}

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
          {sessionHistory.length === 0 ? (
            <p className="px-4 py-3 text-xs text-slate-400">
              {lang === "ko" ? "이번 대화 기록이 여기 표시됩니다." : "Your conversation will appear here."}
            </p>
          ) : (
            sessionHistory.map((h) => (
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
