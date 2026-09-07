"use client";
import { MessageSquare, X, Phone, ExternalLink, Calendar, Megaphone, Monitor, BarChart3, Home } from "lucide-react";
import type { Lang } from "@/lib/types";
import { t } from "@/lib/i18n";
import {
  CONTACT_GROUPS, PHONE_AREA_PREFIX, PORTAL_LINKS, UNIVERSITY_HOME_URL, fullNumber,
} from "@/lib/constants";
import type { ContactLine } from "@/lib/constants";

interface SidebarProps {
  lang: Lang;
  isOpen: boolean;
  onClose: () => void;
}

const LINK_ICONS = { Monitor, BarChart3, Calendar, Megaphone, Home } as const;

/** 내선 하나 = 탭 가능한 칩. 스크린리더에는 전체 번호를 읽어 준다. */
function extChip(l: ContactLine) {
  const number = fullNumber(l.ext);
  return (
    <a
      key={l.ext}
      href={`tel:${number}`}
      aria-label={number}
      className="inline-flex items-center px-2.5 py-1 rounded-lg bg-blue-50 text-blue-700 text-sm font-semibold tabular-nums hover:bg-blue-100 active:bg-blue-200 transition-colors"
    >
      {l.ext}
    </a>
  );
}
const QUICK_LINKS = [
  ...PORTAL_LINKS,
  { key: "link.home", url: UNIVERSITY_HOME_URL, iconName: "Home" },
];

// 2026-09 개편: 대화 기록·새 대화·대화 초기화를 없애고, 챗봇이 답하지 못할 때(장애·범위 밖
// 질문) 학생이 바로 갈 수 있는 연락처와 바로가기를 둔다(장애 대응 보고서 §7.5).
export default function Sidebar({ lang, isOpen, onClose }: SidebarProps) {
  return (
    <>
      {/* Overlay — mobile only. On desktop the sidebar sits in the flex row instead of
          floating over the chat: an overlay there swallowed every click on the page
          (including [다시 시도]) until the student dismissed it. */}
      {isOpen && <div className="fixed inset-0 bg-black/30 z-30 lg:hidden" onClick={onClose} />}

      <aside
        inert={!isOpen}
        className={`h-full w-72 bg-slate-50 border-r border-slate-200 flex-col transition-transform duration-300 fixed top-0 left-0 z-40 flex lg:static lg:z-auto lg:translate-x-0 lg:shrink-0 ${
          isOpen ? "translate-x-0 lg:flex" : "-translate-x-full lg:hidden"
        }`}
      >
        {/* Brand */}
        <div className="p-5 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-blue-600 rounded-xl flex items-center justify-center shadow-lg shadow-blue-200">
              <MessageSquare className="w-6 h-6 text-white" />
            </div>
            <div>
              <span className="block font-bold text-xl tracking-tight leading-tight">{t(lang, "brand.name")}</span>
              <span className="block text-[11px] text-slate-400 font-semibold">{t(lang, "landing.subtitle")}</span>
            </div>
          </div>
          <button onClick={onClose} aria-label={t(lang, "a11y.close")} className="lg:hidden p-1.5 hover:bg-slate-200 rounded-lg text-slate-400">
            <X className="w-5 h-5" />
          </button>
        </div>

        <nav className="flex-grow px-4 overflow-y-auto space-y-6">
          {/* Contacts — 부서별 내선. 국번은 한 번만 적고 번호마다 tel: 링크(모바일에서 바로 통화). */}
          <section>
            <p className="px-2 py-2 text-[10px] font-bold text-slate-400 uppercase tracking-widest">
              {t(lang, "sidebar.contacts")}
            </p>
            <p className="px-2 pb-2 text-[11px] text-slate-500 flex items-center gap-1.5">
              <Phone className="w-3 h-3 text-blue-600 shrink-0" aria-hidden="true" />
              {t(lang, "contact.prefix_hint", { prefix: PHONE_AREA_PREFIX })}
            </p>
            <ul className="bg-white border border-slate-200 rounded-xl divide-y divide-slate-100 shadow-sm">
              {CONTACT_GROUPS.map((g) => {
                const flat = g.lines.every((l) => !l.labelKey);
                return (
                  <li key={g.key} className="px-3 py-2.5">
                    {flat ? (
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-sm font-bold text-slate-800">{t(lang, g.key)}</span>
                        <span className="flex gap-1.5 shrink-0">{g.lines.map((l) => extChip(l))}</span>
                      </div>
                    ) : (
                      <>
                        <span className="block text-sm font-bold text-slate-800 mb-1.5">{t(lang, g.key)}</span>
                        <ul className="space-y-1.5">
                          {g.lines.map((l) => (
                            <li key={l.ext} className="flex items-center justify-between gap-2 pl-2">
                              <span className="text-xs text-slate-600">{t(lang, l.labelKey!)}</span>
                              {extChip(l)}
                            </li>
                          ))}
                        </ul>
                      </>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>

          {/* Links */}
          <section>
            <p className="px-2 py-2 text-[10px] font-bold text-slate-400 uppercase tracking-widest">
              {t(lang, "sidebar.links")}
            </p>
            <ul className="space-y-0.5">
              {QUICK_LINKS.map((l) => {
                const Icon = LINK_ICONS[l.iconName as keyof typeof LINK_ICONS] ?? ExternalLink;
                return (
                  <li key={l.key}>
                    <a
                      href={l.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center gap-3 px-3 py-2 text-sm font-medium text-slate-600 hover:bg-white hover:text-blue-600 rounded-xl transition-all group"
                    >
                      <Icon className="w-4 h-4 opacity-60 group-hover:opacity-100 shrink-0" />
                      <span className="truncate">{t(lang, l.key)}</span>
                      <ExternalLink className="w-3 h-3 ml-auto opacity-0 group-hover:opacity-60 shrink-0" />
                    </a>
                  </li>
                );
              })}
            </ul>
          </section>
        </nav>

        {/* Bottom */}
        <div className="p-4 border-t border-slate-200">
          <p className="text-[11px] text-slate-400 leading-relaxed">{t(lang, "welcome.ai_notice")}</p>
          <p className="text-center text-[10px] text-slate-400 pt-2">BUFS CamChat · v0.1.0</p>
        </div>
      </aside>
    </>
  );
}
