"use client";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Sparkles } from "lucide-react";

// 2026-05-06: 시연 안전성 — 스트리밍 도중에도 디버그용 "검증 경고" 블록 제거.
function stripValidationWarning(t: string): string {
  if (!t) return t;
  return t.replace(/\n*---\n\*검증 경고:\*[\s\S]*?(?=\n---|\n📞|$)/g, "");
}

export default function StreamingMessage({ text }: { text: string }) {
  const cleaned = stripValidationWarning(text);
  const escaped = cleaned.replace(/(?<!\~)\~(?!\~)/g, "\\~");

  return (
    <div className="flex justify-start animate-fade-in">
      <div className="w-10 h-10 rounded-xl bg-blue-600 flex items-center justify-center shrink-0 mr-3 shadow-lg shadow-blue-200 border-2 border-white">
        <Sparkles className="w-5 h-5 text-white" />
      </div>
      <div className="max-w-[85%] lg:max-w-[75%] p-4 bg-slate-50 border border-slate-200 rounded-[1.5rem] rounded-tl-none shadow-sm">
        <div className="prose prose-sm max-w-none whitespace-pre-wrap text-slate-800">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{escaped + " \u258C"}</ReactMarkdown>
        </div>
      </div>
    </div>
  );
}
