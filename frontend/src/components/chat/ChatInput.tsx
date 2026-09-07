"use client";
import { useState, useRef, useCallback } from "react";
import { Send } from "lucide-react";
import type { Lang } from "@/lib/types";
import { t } from "@/lib/i18n";
import { QUESTION_MAX_CHARS } from "@/lib/constants";

interface ChatInputProps {
  lang: Lang;
  onSend: (question: string) => void;
  disabled?: boolean;
}

export default function ChatInput({ lang, onSend, disabled }: ChatInputProps) {
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const handleSubmit = useCallback(() => {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText("");
    inputRef.current?.focus();
  }, [text, disabled, onSend]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  return (
    <div
      className="sticky bottom-0 bg-white/95 backdrop-blur-md border-t border-slate-100 px-4 md:px-6 pt-4 md:pt-6 pb-4 md:pb-6 z-20"
      style={{
        // iOS 홈 바 영역 보호 (notch 기기)
        paddingBottom: "calc(env(safe-area-inset-bottom, 0px) + 1rem)",
      }}
    >
      <div className="max-w-4xl mx-auto space-y-3">
        {/* Input row */}
        <div className="relative flex items-center gap-3">
          <div className="relative flex-grow shadow-xl shadow-blue-50/50">
            <textarea
              ref={inputRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={t(lang, "chat.input_ph")}
              rows={1}
              maxLength={QUESTION_MAX_CHARS}
              disabled={disabled}
              className="w-full bg-slate-50 border border-slate-200 rounded-2xl pl-6 pr-14 py-4 text-[15px] font-semibold resize-none focus:outline-none focus:ring-4 focus:ring-blue-100 focus:bg-white focus:border-blue-400 transition-all disabled:opacity-50"
              style={{ maxHeight: "6rem", minHeight: "3.5rem" }}
            />
            <button
              onClick={handleSubmit}
              disabled={!text.trim() || disabled}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 w-11 h-11 bg-blue-600 hover:bg-blue-700 disabled:bg-slate-300 text-white rounded-xl flex items-center justify-center transition-all shadow-lg shadow-blue-200 active:scale-90"
            >
              <Send className="w-5 h-5" />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
