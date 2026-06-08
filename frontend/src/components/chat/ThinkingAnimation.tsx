"use client";
import { Sparkles } from "lucide-react";
import type { Lang } from "@/lib/types";
import { t } from "@/lib/i18n";

export default function ThinkingAnimation({ lang }: { lang: Lang }) {
  return (
    <div className="flex justify-start animate-fade-in">
      <div className="w-10 h-10 rounded-xl bg-blue-100 flex items-center justify-center shrink-0 mr-3">
        <Sparkles className="w-5 h-5 text-blue-600 animate-sparkle" />
      </div>
      <div className="bg-slate-50 border border-slate-200 p-5 rounded-[1.5rem] rounded-tl-none shadow-sm">
        <div className="flex flex-col items-center gap-3">
          <div className="flex gap-2">
            <div className="w-2.5 h-2.5 bg-blue-400 rounded-full animate-bounce" />
            <div className="w-2.5 h-2.5 bg-blue-400 rounded-full animate-bounce [animation-delay:0.2s]" />
            <div className="w-2.5 h-2.5 bg-blue-400 rounded-full animate-bounce [animation-delay:0.4s]" />
          </div>
          <p className="text-xs font-semibold text-slate-500">
            {t(lang, "chat.thinking")}<span className="animate-dots" />
          </p>
          <p className="text-[10px] text-slate-400">{t(lang, "chat.thinking_sub")}</p>
        </div>
      </div>
    </div>
  );
}
