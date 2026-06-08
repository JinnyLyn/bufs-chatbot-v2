"use client";
import { Sparkles } from "lucide-react";
import type { Lang } from "@/lib/types";
import { t } from "@/lib/i18n";

interface WelcomeScreenProps {
  lang: Lang;
  onSelect: (q: string) => void;
}

// A few starter questions for the empty state. `q` is the full question sent;
// `label` is the short chip text.
const EXAMPLES = [
  { label: "qf.register", q: "qf.register_q" },
  { label: "qf.schedule", q: "qf.schedule_q" },
  { label: "qf.grades", q: "qf.grades_q" },
  { label: "qf.faq", q: "qf.faq_q" },
];

export default function WelcomeScreen({ lang, onSelect }: WelcomeScreenProps) {
  return (
    <div className="py-8 md:py-12 space-y-10 animate-fade-in">
      {/* Hero */}
      <div className="space-y-5 text-center lg:text-left">
        <div className="inline-flex items-center gap-2 px-4 py-2 bg-blue-50 text-blue-600 rounded-2xl font-bold text-xs uppercase tracking-widest shadow-sm border border-blue-100">
          <Sparkles className="w-4 h-4" /> Agentic RAG Assistant
        </div>
        <h1 className="text-3xl lg:text-5xl font-black text-slate-900 tracking-tight leading-tight">
          {t(lang, "welcome.hero_title")}
          <br />
          <span className="text-slate-400">{t(lang, "welcome.hero_sub")}</span>
        </h1>
        <p className="text-slate-500 font-semibold text-base md:text-lg max-w-xl mx-auto lg:mx-0 leading-relaxed">
          {t(lang, "welcome.hero_desc")}
        </p>
      </div>

      {/* Starter questions */}
      <div className="flex flex-wrap gap-2 justify-center lg:justify-start">
        {EXAMPLES.map((e) => (
          <button
            key={e.label}
            onClick={() => onSelect(t(lang, e.q))}
            className="px-4 py-2 bg-slate-50 hover:bg-blue-600 hover:text-white border border-slate-200 rounded-full text-sm font-semibold text-slate-600 transition-all shadow-sm active:scale-95"
          >
            {t(lang, e.label)}
          </button>
        ))}
      </div>

      <p className="text-center lg:text-left text-xs text-slate-400 font-semibold">
        {t(lang, "chat.welcome_hint")}
      </p>
    </div>
  );
}
