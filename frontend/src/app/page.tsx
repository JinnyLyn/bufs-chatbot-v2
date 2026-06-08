"use client";
import { useState } from "react";
import { AlertTriangle } from "lucide-react";

export default function LangSelect() {
  const [agreed, setAgreed] = useState(false);

  return (
    <div className="flex flex-col items-center justify-center min-h-screen bg-main px-4">
      <div className="text-6xl mb-4">{"\uD83C\uDF93"}</div>
      <h1 className="text-2xl font-bold text-navy mb-1">BUFS Academic Info AI</h1>
      <p className="text-sm text-text-sub mb-8">
        언어를 선택해주세요 / Please select your language
      </p>

      {/* Disclaimer */}
      <div className="w-full max-w-md mb-6">
        <div className="bg-amber-50 border border-amber-300 rounded-xl p-4 shadow-sm">
          <div className="flex items-start gap-2.5 mb-3">
            <AlertTriangle className="w-5 h-5 text-amber-600 shrink-0 mt-0.5" />
            <p className="text-xs text-amber-900 leading-relaxed font-medium">
              본 서비스의 답변은 AI에 의해 자동 생성되며, 공식 학사 규정과 다를 수 있습니다.
              중요한 사항은 반드시 학사지원팀(051-509-5182)에 확인해 주세요.
            </p>
          </div>
          <div className="flex items-start gap-2.5">
            <AlertTriangle className="w-5 h-5 text-amber-600 shrink-0 mt-0.5 opacity-0" />
            <p className="text-xs text-amber-800 leading-relaxed">
              Responses are AI-generated and may differ from official academic regulations.
              Please verify important matters with the Academic Affairs Office (+82-51-509-5182).
            </p>
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
            위 내용을 확인하였으며 동의합니다 / I have read and agree to the above
          </span>
        </label>
      </div>

      {/* Language buttons */}
      <div className="flex gap-4">
        {agreed ? (
          <>
            <a
              href="/ko/chat"
              className="w-40 py-3.5 rounded-xl bg-navy text-white font-semibold text-lg shadow-md hover:shadow-lg hover:scale-[1.02] transition-all text-center no-underline"
            >
              한국어
            </a>
            <a
              href="/en/chat"
              className="w-40 py-3.5 rounded-xl bg-white text-navy font-semibold text-lg border-2 border-navy shadow-md hover:shadow-lg hover:scale-[1.02] transition-all text-center no-underline"
            >
              English
            </a>
          </>
        ) : (
          <>
            <span className="w-40 py-3.5 rounded-xl bg-slate-300 text-white font-semibold text-lg shadow-sm text-center cursor-not-allowed select-none">
              한국어
            </span>
            <span className="w-40 py-3.5 rounded-xl bg-white text-slate-300 font-semibold text-lg border-2 border-slate-200 shadow-sm text-center cursor-not-allowed select-none">
              English
            </span>
          </>
        )}
      </div>
    </div>
  );
}
