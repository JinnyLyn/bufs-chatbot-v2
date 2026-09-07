"use client";
import { Sparkles } from "lucide-react";
import { STREAM_STAGES } from "@/lib/types";
import type { Lang, StreamProgress } from "@/lib/types";
import { t } from "@/lib/i18n";

// 답변 첫 글자까지 4~17초가 걸린다(2026-09-01 실측). 그 사이 화면에 아무 정보가 없는 것이
// "느리다"는 피드백의 실체라, 서버가 보내주는 단계를 그대로 보여준다. progress 가 없으면
// (구버전 서버·이벤트 유실) 기존 문구로 그대로 떨어진다.
export default function ThinkingAnimation({
  lang,
  progress,
  stalled = false,
}: {
  lang: Lang;
  progress?: StreamProgress | null;
  /** 45초 동안 진행 신호가 없었다(보고서 §7.1). 요청은 계속 진행 중이다. */
  stalled?: boolean;
}) {
  // 서버가 나중에 새 단계를 추가해도 화면에 "[chat.stage.xxx]" 가 뜨지 않도록,
  // 아는 단계만 문구로 바꾸고 나머지는 기존 문구로 떨어뜨린다(t 는 미지의 키에 [key] 를 준다).
  const known = progress && (STREAM_STAGES as readonly string[]).includes(progress.stage);
  const headline = stalled
    ? t(lang, "chat.slow")
    : known
      ? t(lang, `chat.stage.${progress!.stage}`)
      : t(lang, "chat.thinking");
  const sub =
    progress && progress.searches > 0
      ? t(lang, "chat.stage.searches", { n: progress.searches })
      : t(lang, "chat.thinking_sub");

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
          <p className={`text-xs font-semibold ${stalled ? "text-amber-700" : "text-slate-500"}`}>
            {/* aria-live 는 텍스트에만 건다. 점 애니메이션은 ::after content 가 1.8초마다
                바뀌어, 같은 영역에 두면 대기 내내 스크린리더가 반복해서 읽는다. */}
            <span aria-live="polite">{headline}</span>
            <span className="animate-dots" aria-hidden="true" />
          </p>
          <p className="text-[10px] text-slate-400">{sub}</p>
        </div>
      </div>
    </div>
  );
}
