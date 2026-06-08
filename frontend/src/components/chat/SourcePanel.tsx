"use client";
import { useState } from "react";
import { ChevronDown, ChevronUp, FileText, ExternalLink } from "lucide-react";
import type { Lang, SearchResultItem, SourceURL } from "@/lib/types";
import { t } from "@/lib/i18n";

interface SourcePanelProps {
  lang: Lang;
  results?: SearchResultItem[];
  sourceUrls?: SourceURL[];
}

function sourceLabel(r: SearchResultItem): string {
  if (r.source) {
    const name = r.source.split("/").pop() || r.source;
    return `${name}${r.page_number ? ` p.${r.page_number}` : ""}`;
  }
  return r.doc_type || "source";
}

export default function SourcePanel({ lang, results, sourceUrls }: SourcePanelProps) {
  const [open, setOpen] = useState(false);

  const contextResults = results?.filter((r) => r.in_context)?.slice(0, 5) ?? [];
  if (contextResults.length === 0 && (!sourceUrls || sourceUrls.length === 0)) return null;

  // De-duplicate by source + a short text fingerprint.
  const seen = new Set<string>();
  const deduped = contextResults.filter((r) => {
    const key = `${r.source}:${(r.text || "").slice(0, 40)}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  return (
    <div className="mt-2 ml-13">
      <button
        onClick={() => setOpen(!open)}
        className="text-xs text-blue-600 hover:text-blue-800 font-semibold flex items-center gap-1 transition-colors"
      >
        <FileText className="w-3.5 h-3.5" />
        {t(lang, "source.panel")}
        {open ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
      </button>

      {open && (
        <div className="mt-2 space-y-2 border-t border-slate-200 pt-2 animate-fade-in">
          {deduped.map((r, i) => (
            <div key={i} className="text-xs rounded-lg border border-slate-200 overflow-hidden">
              <div className="p-2.5 bg-slate-50">
                <span className="text-[10px] font-bold text-slate-400 uppercase">{sourceLabel(r)}</span>
                {r.section_path && (
                  <span className="text-[10px] text-slate-400 ml-1.5">{r.section_path}</span>
                )}
                <p className="text-slate-600 mt-1 line-clamp-3">{(r.text || "").slice(0, 240)}</p>
              </div>

              {r.source_url && (
                <div className="px-2.5 pb-2 bg-slate-50">
                  <a
                    href={r.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-[10px] text-blue-500 hover:underline flex items-center gap-0.5"
                  >
                    <ExternalLink className="w-3 h-3" />
                    {r.title || r.source_url}
                  </a>
                </div>
              )}
            </div>
          ))}

          {sourceUrls && sourceUrls.length > 0 && (
            <div className="pt-1">
              <p className="text-[10px] font-bold text-slate-400 uppercase mb-1">{t(lang, "source.related")}</p>
              {sourceUrls.map((s, i) => (
                <a
                  key={i}
                  href={s.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-xs text-blue-600 hover:underline flex items-center gap-1 py-0.5"
                >
                  <ExternalLink className="w-3 h-3" />
                  {s.title}
                </a>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
