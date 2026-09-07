"use client";
import { Sparkles, CheckCircle2, Info } from "lucide-react";
import type { Lang } from "@/lib/types";
import { t } from "@/lib/i18n";

interface WelcomeScreenProps {
  lang: Lang;
}

// 빈 화면 = 정보 가이드. 예시 질문 버튼은 없앴다(2026-09 개편): 학사정보 전용 챗봇이 무엇을
// 답하고 무엇을 답하지 않는지 먼저 보여 주고, 질문은 입력창으로만 받는다.
export default function WelcomeScreen({ lang }: WelcomeScreenProps) {
  const scope = t(lang, "welcome.scope_items").split("|");

  return (
    <div className="py-8 md:py-12 space-y-8 animate-fade-in">
      {/* Hero */}
      <div className="space-y-4 text-center lg:text-left">
        <div className="inline-flex items-center gap-2 px-4 py-2 bg-blue-50 text-blue-600 rounded-2xl font-bold text-xs tracking-wide shadow-sm border border-blue-100">
          <Sparkles className="w-4 h-4" /> {t(lang, "welcome.badge")}
        </div>
        <h1 className="text-3xl lg:text-5xl font-black text-slate-900 tracking-tight leading-tight">
          {t(lang, "landing.title")}
          <br />
          <span className="text-slate-400 text-2xl lg:text-4xl">{t(lang, "landing.subtitle")}</span>
        </h1>
        <p className="text-slate-500 font-semibold text-base md:text-lg max-w-xl mx-auto lg:mx-0 leading-relaxed">
          {t(lang, "landing.tagline")} {t(lang, "welcome.desc")}
        </p>
      </div>

      {/* Guide */}
      <div className="grid gap-4 md:grid-cols-2">
        <section className="rounded-2xl border border-slate-200 bg-slate-50 p-5">
          <h2 className="flex items-center gap-2 text-sm font-bold text-slate-800 mb-3">
            <CheckCircle2 className="w-4 h-4 text-green-600" />
            {t(lang, "welcome.scope_title")}
          </h2>
          <ul className="flex flex-wrap gap-2">
            {scope.map((item) => (
              <li
                key={item}
                className="px-3 py-1.5 bg-white border border-slate-200 rounded-full text-xs font-semibold text-slate-600"
              >
                {item}
              </li>
            ))}
          </ul>
        </section>
        <section className="rounded-2xl border border-amber-200 bg-amber-50 p-5">
          <h2 className="flex items-center gap-2 text-sm font-bold text-slate-800 mb-3">
            <Info className="w-4 h-4 text-amber-600" />
            {t(lang, "welcome.limit_title")}
          </h2>
          <p className="text-xs text-slate-700 leading-relaxed">{t(lang, "welcome.limit_desc")}</p>
        </section>
      </div>

      <div className="text-center lg:text-left space-y-1">
        <p className="text-xs text-slate-400 font-semibold">{t(lang, "welcome.hint")}</p>
        <p className="text-[11px] text-slate-400">{t(lang, "welcome.ai_notice")}</p>
      </div>
    </div>
  );
}
