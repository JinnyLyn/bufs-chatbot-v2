"use client";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Sparkles } from "lucide-react";
import type { Lang } from "@/lib/types";
import { t } from "@/lib/i18n";

// 2026-05-06: 시연 안전성 — 스트리밍 도중에도 디버그용 "검증 경고" 블록 제거.
function stripValidationWarning(t: string): string {
  if (!t) return t;
  return t.replace(/\n*---\n\*검증 경고:\*[\s\S]*?(?=\n---|\n📞|$)/g, "");
}

export default function StreamingMessage({
  text,
  lang = "ko",
  stalled = false,
}: {
  text: string;
  lang?: Lang;
  /** 토큰이 오다가 45초 동안 멈췄다. 받은 만큼은 그대로 두고 아래에 안내만 붙인다. */
  stalled?: boolean;
}) {
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
        {/* 항상 마운트해 두고 문구만 바꾼다. 이미 채워진 채로 나타나는 live region 은
            스크린리더가 대개 읽지 않는다(ThinkingAnimation 과 같은 방식). */}
        <p aria-live="polite" className={stalled ? "mt-3 text-xs font-semibold text-amber-700" : "sr-only"}>
          {stalled ? t(lang, "chat.slow") : ""}
        </p>
      </div>
    </div>
  );
}
