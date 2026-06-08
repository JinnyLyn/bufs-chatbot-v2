"use client";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Sparkles, User } from "lucide-react";
import type { ChatMessage as Msg } from "@/lib/types";

// 2026-05-06: 시연 안전성 — 백엔드 응답에 포함된 디버그용 "검증 경고" 블록을
// UI 렌더링 시 제거. 백엔드 응답 자체에는 보존되어 관리자 로그·디버그에 활용 가능.
// 패턴: "---\n*검증 경고:* ..." 블록을 다음 구분선(---) 또는 연락처(📞) 직전까지 제거.
function stripValidationWarning(text: string): string {
  if (!text) return text;
  return text.replace(
    /\n*---\n\*검증 경고:\*[\s\S]*?(?=\n---|\n📞|$)/g,
    "",
  );
}

export default function ChatMessage({ msg }: { msg: Msg }) {
  const isUser = msg.role === "user";
  const cleaned = isUser ? msg.content : stripValidationWarning(msg.content);

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} animate-slide-up`}>
      {!isUser && (
        <div className="w-10 h-10 rounded-xl bg-blue-600 flex items-center justify-center shrink-0 mr-3 shadow-lg shadow-blue-200 border-2 border-white">
          <Sparkles className="w-5 h-5 text-white" />
        </div>
      )}
      <div
        className={`max-w-[85%] lg:max-w-[75%] p-4 rounded-[1.5rem] shadow-sm leading-relaxed ${
          isUser
            ? "bg-blue-600 text-white rounded-tr-none font-semibold"
            : "bg-slate-50 border border-slate-200 text-slate-800 rounded-tl-none"
        }`}
      >
        {isUser ? (
          <p className="text-[15px] whitespace-pre-wrap">{cleaned}</p>
        ) : (
          <div className="prose prose-sm max-w-none whitespace-pre-wrap text-slate-800 prose-headings:text-slate-900 prose-a:text-blue-600">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{cleaned}</ReactMarkdown>
          </div>
        )}
      </div>
      {isUser && (
        <div className="w-10 h-10 rounded-xl bg-blue-50 border border-blue-100 flex items-center justify-center shrink-0 ml-3 hidden md:flex">
          <User className="w-5 h-5 text-blue-600" />
        </div>
      )}
    </div>
  );
}
