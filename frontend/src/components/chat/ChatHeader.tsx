"use client";
import { Menu, Globe, MessageSquare } from "lucide-react";
import type { Lang } from "@/lib/types";
import { t } from "@/lib/i18n";

interface ChatHeaderProps {
  lang: Lang;
  title?: string;
  onToggleSidebar: () => void;
  onToggleLang: () => void;
}

export default function ChatHeader({ lang, title, onToggleSidebar, onToggleLang }: ChatHeaderProps) {
  const displayTitle = title || t(lang, "brand.name");

  return (
    <header className="h-14 md:h-16 border-b border-slate-100 px-4 md:px-6 flex justify-between items-center bg-white/80 backdrop-blur-md sticky top-0 z-10 shrink-0">
      {/* Left */}
      <div className="flex items-center gap-3">
        <button
          onClick={onToggleSidebar}
          className="p-2 hover:bg-slate-100 rounded-lg text-slate-500 transition-colors"
        >
          <Menu className="w-5 h-5" />
        </button>

        <div className="lg:hidden w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center shadow-md">
          <MessageSquare className="w-5 h-5 text-white" />
        </div>

        <div>
          <h2 className="font-bold text-slate-900 text-sm md:text-base tracking-tight">{displayTitle}</h2>
          <div className="flex items-center gap-1.5">
            <span className="w-2 h-2 bg-green-500 rounded-full animate-pulse shadow-[0_0_5px_rgba(34,197,94,0.5)]" />
            <span className="text-[10px] font-bold text-slate-400 uppercase tracking-tighter">
              {t(lang, "header.ai_active")}
            </span>
          </div>
        </div>
      </div>

      {/* Right */}
      <div className="flex items-center gap-2 md:gap-3">
        <button
          onClick={onToggleLang}
          className="flex items-center gap-1.5 px-2.5 py-1.5 bg-slate-100 hover:bg-slate-200 rounded-full text-[11px] font-bold transition-all text-slate-700"
        >
          <Globe className="w-3.5 h-3.5" /> {lang === "ko" ? "EN" : "KO"}
        </button>
      </div>
    </header>
  );
}
