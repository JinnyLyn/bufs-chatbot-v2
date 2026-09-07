"use client";
import { memo, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Sparkles, User, AlertTriangle, RotateCw } from "lucide-react";
import type { ChatMessage as Msg, Lang } from "@/lib/types";
import { t } from "@/lib/i18n";
import { EMERGENCY_CONTACTS, ACADEMIC_CALENDAR_URL } from "@/lib/constants";

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

/** `{key}` 자리에 링크 같은 노드를 끼워 넣는다. t() 는 문자열만 돌려주기 때문. */
function splitAt(template: string, key: string): [string, string] {
  const [head, ...rest] = template.split(`{${key}}`);
  return [head, rest.join(`{${key}}`)];
}

function remainingSeconds(until: number | null): number {
  return until ? Math.max(0, Math.ceil((until - Date.now()) / 1000)) : 0;
}

/** 잠금 해제 시각까지 남은 초. 이 말풍선 안에서만 1초마다 다시 그린다. */
function useCountdown(until: number | null): number {
  const [secs, setSecs] = useState(() => remainingSeconds(until));
  useEffect(() => {
    if (!until) return;
    const id = setInterval(() => {
      const r = remainingSeconds(until);
      setSecs(r);
      if (r <= 0) clearInterval(id);
    }, 1000);
    return () => clearInterval(id);
  }, [until]);
  // 훅이 잠금을 풀면(until → null) 마지막 tick 이 0 에 닿지 못했어도 즉시 0 이어야 한다.
  return until ? secs : 0;
}

interface Props {
  msg: Msg;
  lang?: Lang;
  /** 이 말풍선이 마지막 안내이고 다시 보낼 질문이 있을 때만 버튼을 그린다. */
  showRetry?: boolean;
  /** 혼잡 잠금이 풀리는 시각(ms). null 이면 바로 누를 수 있다. 마지막 말풍선에만 넘긴다. */
  retryUntil?: number | null;
  onRetry?: () => void;
}

// 응답 실패 안내(보고서 §7.2~7.5). 문구 아래에 대체 문의 경로를 항상 함께 보여준다.
function ErrorBubble({ msg, lang, showRetry, retryUntil = null, onRetry }: Props & { lang: Lang }) {
  const academic = EMERGENCY_CONTACTS.academic;
  const [telHead, telTail] = splitAt(t(lang, "chat.emergency"), "tel");
  const [calHead, calTail] = splitAt(t(lang, "chat.emergency_calendar"), "link");
  const waitS = useCountdown(retryUntil);
  const waiting = waitS > 0;

  return (
    <div className="flex justify-start animate-slide-up" data-testid="chat-error">
      <div className="w-10 h-10 rounded-xl bg-amber-100 border-2 border-white flex items-center justify-center shrink-0 mr-3 shadow-sm">
        <AlertTriangle className="w-5 h-5 text-amber-600" />
      </div>
      <div className="max-w-[85%] lg:max-w-[75%] p-4 rounded-[1.5rem] rounded-tl-none bg-amber-50 border border-amber-200 text-slate-800 shadow-sm space-y-3">
        {/* aria-live 는 고정 문구에만 건다. 카운트다운을 같은 영역에 두면 aria-atomic 때문에
            연락처까지 1초마다 다시 읽힌다(ThinkingAnimation 의 점 애니메이션과 같은 문제). */}
        <p className="text-[15px] font-semibold leading-relaxed" role="alert">{msg.content}</p>
        <div className="text-xs text-slate-600 leading-relaxed space-y-0.5">
          <p>
            {telHead}
            <a href={`tel:${academic.tel}`} className="font-semibold text-blue-700 underline underline-offset-2">
              {academic.display}
            </a>
            {telTail}
          </p>
          <p>
            {calHead}
            <a
              href={ACADEMIC_CALENDAR_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="font-semibold text-blue-700 underline underline-offset-2"
            >
              {t(lang, "chat.emergency_calendar_link")}
            </a>
            {calTail}
          </p>
        </div>
        {showRetry && onRetry && (
          <button
            type="button"
            onClick={onRetry}
            disabled={waiting}
            aria-label={t(lang, "chat.retry")}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-blue-600 hover:bg-blue-700 disabled:bg-slate-300 disabled:cursor-not-allowed text-white text-sm font-semibold shadow-sm transition-colors"
          >
            <RotateCw className="w-4 h-4" aria-hidden="true" />
            <span aria-hidden="true">
              {waiting ? t(lang, "chat.retry_in", { s: waitS }) : t(lang, "chat.retry")}
            </span>
          </button>
        )}
      </div>
    </div>
  );
}

function ChatMessage(props: Props) {
  const { msg, lang = "ko" } = props;
  if (msg.errorKind) return <ErrorBubble {...props} lang={lang} />;

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
        {msg.partial && (
          <p className="mt-2 text-[11px] font-semibold text-amber-700">{t(lang, "chat.partial")}</p>
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

// 혼잡 잠금 카운트다운은 마지막 말풍선 안에서만 돈다. memo 로 나머지 말풍선(마크다운 파싱)이
// 그 1초 주기에 끌려 다시 그려지지 않게 한다.
export default memo(ChatMessage);
