"use client";
import { useState } from "react";
import { AlertTriangle } from "lucide-react";
import { t } from "@/lib/i18n";
import { EMERGENCY_CONTACTS } from "@/lib/constants";

// 랜딩 = 안내 + 동의 + 시작. 2026-09 개편으로 언어 선택을 없애고 한국어 챗으로만 들어간다.
// (/en/chat 경로는 남아 있지만 여기서 안내하지 않는다.)
export default function Landing() {
  const [agreed, setAgreed] = useState(false);
  const lang = "ko" as const;

  return (
    <div className="flex flex-col items-center justify-center min-h-screen bg-main px-4">
      <div className="text-6xl mb-4">{"🎓"}</div>
      <h1 className="text-3xl font-black text-navy mb-1 tracking-tight">{t(lang, "landing.title")}</h1>
      <p className="text-base font-semibold text-text mb-1">{t(lang, "landing.subtitle")}</p>
      <p className="text-sm text-text-sub mb-8">{t(lang, "landing.tagline")}</p>

      {/* Disclaimer */}
      <div className="w-full max-w-md mb-6">
        <div className="bg-amber-50 border border-amber-300 rounded-xl p-4 shadow-sm">
          <div className="flex items-start gap-2.5">
            <AlertTriangle className="w-5 h-5 text-amber-600 shrink-0 mt-0.5" />
            <p className="text-xs text-amber-900 leading-relaxed font-medium">{t(lang, "landing.disclaimer", { tel: EMERGENCY_CONTACTS.academic.display })}</p>
          </div>
        </div>

        <label className="flex items-center gap-2.5 mt-3 cursor-pointer select-none group">
          <input
            type="checkbox"
            checked={agreed}
            onChange={(e) => setAgreed(e.target.checked)}
            className="w-4.5 h-4.5 rounded border-slate-300 text-blue-600 accent-blue-600 cursor-pointer"
          />
          <span className="text-sm text-slate-700 group-hover:text-slate-900 transition-colors">
            {t(lang, "landing.agree")}
          </span>
        </label>
      </div>

      {agreed ? (
        <a
          href="/ko/chat"
          className="w-56 py-3.5 rounded-xl bg-navy text-white font-semibold text-lg shadow-md hover:shadow-lg hover:scale-[1.02] transition-all text-center no-underline"
        >
          {t(lang, "landing.start")}
        </a>
      ) : (
        <span
          aria-disabled="true"
          className="w-56 py-3.5 rounded-xl bg-slate-300 text-white font-semibold text-lg shadow-sm text-center cursor-not-allowed select-none"
        >
          {t(lang, "landing.start")}
        </span>
      )}
    </div>
  );
}
