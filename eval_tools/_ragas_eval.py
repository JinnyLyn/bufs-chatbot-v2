"""RAGAS-style eval of the NEW agentic-rag chatbot, LLM-as-judge.

Reuses bufs-chatbot's 5 metric judge-prompts (eval_ragas.py) verbatim. Generation =
the new chatbot's SSE API (answer + retrieved contexts). Judge = Gemini (REST via requests
+ win-ca-bundle, the Norton-TLS-safe path) OR a local Ollama model (different from the
qwen3.5:9b generator, to avoid self-bias).

Run:
  # Gemini judge (needs a working GOOGLE_API_KEY in env):
  python _ragas_eval.py --judge gemini --model gemini-2.5-flash --n 25
  # Local judge (free, no external key):
  python _ragas_eval.py --judge ollama --model exaone3.5:7.8b --n 25
"""
import argparse, json, os, re, sys, time, urllib.parse
import requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qa_scorer  # in-repo golden dataset loader (eval_tools/datasets/qa_dataset.json)
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

NEW_BASE = "http://localhost:8000"
OLLAMA = "http://localhost:11435"
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Gemini-only TLS bundle for networks behind a corporate/Norton MITM proxy: point
# REQUESTS_CA_BUNDLE at a PEM file there. Unset (Linux CI / normal nets) → verify via certifi.
CA = os.environ.get("REQUESTS_CA_BUNDLE") or None

# ── bufs's 5 RAGAS judge prompts (verbatim) ─────────────────────────────────
FAITHFULNESS_SYSTEM = """당신은 RAG 시스템 평가 전문가입니다.
생성된 답변이 오직 검색된 컨텍스트 내의 정보에만 근거하는지 평가합니다.
점수 기준: 1.0 모든 주장이 컨텍스트에 근거 / 0.8~0.9 대부분 근거(사소한 형식변환) / 0.5~0.7 일부 근거없음 / 0.0~0.3 핵심을 지어냄.
중요: "컨텍스트에 정보가 없어 문의 바랍니다"는 환각이 아닙니다(실제 없으면 1.0).
반드시 JSON만: {"score": 0.0, "reason": "한 줄"}"""
ANSWER_RELEVANCY_SYSTEM = """당신은 RAG 시스템 평가 전문가입니다.
답변이 질문 의도에 부합하는지 평가합니다. 핵심 정보(날짜/숫자/조건)를 정확히 포함하면 0.8 이상.
0.9~1.0 핵심 정확+한정어 반영 / 0.8 핵심 정확 / 0.6~0.7 부분 / 0.4~0.5 "문의하세요"만 / 0.0~0.3 무관.
반드시 JSON만: {"score": 0.0, "reason": "한 줄"}"""
CONTEXT_PRECISION_SYSTEM = """당신은 RAG 시스템 평가 전문가입니다.
검색된 컨텍스트 중 질문에 답하는 데 유용한 정보의 비율을 평가합니다.
1.0 전부 관련 / 0.7~0.9 핵심+일부노이즈 / 0.4~0.6 반반 / 0.1~0.3 대부분 무관 / 0.0 전혀 무관.
반드시 JSON만: {"score": 0.0, "reason": "한 줄"}"""
CONTEXT_RECALL_SYSTEM = """당신은 RAG 시스템 평가 전문가입니다.
정답(reference)을 도출할 근거가 컨텍스트에 포함되어 있는지 평가합니다. 표현이 달라도 같은 사실이면 포함.
0.9~1.0 모든 핵심 있음 / 0.8 핵심있음+세부누락 / 0.5~0.7 일부 / 0.2~0.4 부족 / 0.0 없음.
반드시 JSON만: {"score": 0.0, "reason": "한 줄"}"""
ANSWER_CORRECTNESS_SYSTEM = """당신은 RAG 시스템 평가 전문가입니다.
생성된 답변이 정답(reference)과 얼마나 일치하는지 평가합니다.
1.0 핵심(날짜/숫자/조건) 모두 일치 / 0.7~0.9 핵심맞고 세부누락 / 0.4~0.6 일부 / 0.1~0.3 대부분 불일치 / 0.0 완전 불일치.
반드시 JSON만: {"score": 0.0, "reason": "한 줄"}"""

METRIC_CONFIG = {
 "faithfulness": (FAITHFULNESS_SYSTEM, "[검색된 컨텍스트]\n{context}\n\n[생성된 답변]\n{answer}\n\n답변이 컨텍스트에만 근거하는지 평가해 JSON으로.", "Faithfulness 성실성"),
 "answer_relevancy": (ANSWER_RELEVANCY_SYSTEM, "[질문]\n{question}\n\n[생성된 답변]\n{answer}\n\n답변이 질문 의도에 부합하는지 평가해 JSON으로.", "Answer Relevancy 관련성"),
 "context_precision": (CONTEXT_PRECISION_SYSTEM, "[질문]\n{question}\n\n[정답]\n{reference}\n\n[검색된 컨텍스트]\n{context}\n\n컨텍스트가 유용한지 평가해 JSON으로.", "Context Precision 정밀도"),
 "context_recall": (CONTEXT_RECALL_SYSTEM, "[정답]\n{reference}\n\n[검색된 컨텍스트]\n{context}\n\n정답 근거가 컨텍스트에 있는지 평가해 JSON으로.", "Context Recall 재현율"),
 "answer_correctness": (ANSWER_CORRECTNESS_SYSTEM, "[질문]\n{question}\n\n[정답]\n{reference}\n\n[생성된 답변]\n{answer}\n\n답변이 정답과 일치하는지 평가해 JSON으로.", "Answer Correctness 정확도"),
}
METRICS = list(METRIC_CONFIG.keys())


def extract_score(text):
    m = re.search(r"\{[^{}]*\}", text or "", re.DOTALL)
    if m:
        try:
            o = json.loads(m.group())
            return max(0.0, min(1.0, float(o.get("score", -1)))), o.get("reason", "")
        except Exception:
            pass
    return -1.0, ""


def gemini_judge(system, prompt, model):
    key = os.environ.get("GOOGLE_API_KEY", "")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {"system_instruction": {"parts": [{"text": system}]},
               "contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0, "maxOutputTokens": 256, "thinkingConfig": {"thinkingBudget": 0}}}
    for attempt in range(5):
        r = requests.post(url, json=payload, timeout=60, verify=CA)
        if r.status_code == 200:
            d = r.json()
            try:
                return "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"]).strip()
            except Exception:
                return ""
        if r.status_code == 429 and attempt < 4:
            time.sleep(min(15 * (2 ** attempt), 90)); continue
        raise RuntimeError(f"Gemini {r.status_code}: {r.text[:160]}")


def ollama_judge(system, prompt, model):
    r = requests.post(f"{OLLAMA}/api/chat", timeout=120, json={
        "model": model, "stream": False, "options": {"temperature": 0, "num_predict": 256},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


def ask_chatbot(q, timeout=300):
    sid = requests.post(NEW_BASE + "/api/session", json={"lang": "ko"}, timeout=30).json()["session_id"]
    url = NEW_BASE + "/api/chat/stream?" + urllib.parse.urlencode({"session_id": sid, "question": q})
    done, event = None, None
    with requests.get(url, stream=True, timeout=timeout) as resp:
        for line in resp.iter_lines(decode_unicode=True):
            if line is None: continue
            if line.startswith("event:"): event = line[6:].strip()
            elif line.startswith("data:") and event == "done": done = json.loads(line[5:].strip())
            elif line == "": event = None
    done = done or {}
    ctx = "\n\n".join(r.get("text", "") for r in (done.get("results") or []) if r.get("text"))
    return done.get("answer", ""), ctx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", choices=["gemini", "ollama"], default="gemini")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--delay", type=float, default=4.0, help="sec between judge calls (API rate limit)")
    ap.add_argument("--dataset", default=None, help="override dataset path (default: in-repo qa_dataset.json)")
    args = ap.parse_args()
    judge = (lambda s, p: gemini_judge(s, p, args.model)) if args.judge == "gemini" else (lambda s, p: ollama_judge(s, p, args.model))

    data = qa_scorer.load_dataset(args.dataset)[:args.n]
    print(f"RAGAS eval | judge={args.judge}:{args.model} | n={len(data)} | gen=new chatbot(qwen3.5:9b)", flush=True)

    # Phase 1: generate ALL first (qwen3.5:9b stays loaded — no per-question model swap)
    print("Phase 1 — generation (new chatbot / qwen3.5:9b)", flush=True)
    gen = []
    for i, r in enumerate(data, 1):
        try:
            ans, ctx = ask_chatbot(r["question"])
        except Exception as e:
            ans, ctx = f"(ERR {e})", ""
        gen.append({**r, "_ans": ans, "_ctx": ctx})
        print(f"  gen [{i:2}/{len(data)}] {r['id']:5} ans={len(ans)}자 ctx={len(ctx)}", flush=True)

    # Phase 2: judge ALL (judge model loads once)
    print(f"\nPhase 2 — judge ({args.judge}:{args.model})", flush=True)
    results = []
    for i, g in enumerate(gen, 1):
        q, ref, ans, ctx = g["question"], g.get("expected_answer", ""), g["_ans"], g["_ctx"]
        row = {"id": g["id"], "intent": g.get("gold_intent"), "question": q, "reference": ref,
               "answer": ans[:600], "context_preview": ctx[:300]}
        for j, m in enumerate(METRICS):
            sysp, tmpl, _ = METRIC_CONFIG[m]
            prompt = tmpl.format(question=q[:500], context=ctx[:1200], answer=ans[:500], reference=ref[:300])
            try:
                sc, _reason = extract_score(judge(sysp, prompt))
            except Exception as e:
                sc = -1.0
                if "429" in str(e):
                    print(f"   judge error: {str(e)[:120]}", flush=True)
            row[m] = sc
            if args.judge == "gemini" and j < len(METRICS) - 1:
                time.sleep(args.delay)
        sc_str = " ".join(f"{m.split('_')[0][:4]}={row[m]:.2f}" for m in METRICS)
        print(f"  judge [{i:2}/{len(data)}] {g['id']:5} {sc_str}", flush=True)
        results.append(row)

    summary = {}
    for m in METRICS:
        vals = [r[m] for r in results if r.get(m, -1) >= 0]
        summary[m] = round(sum(vals) / len(vals), 4) if vals else None
    valid = [v for v in summary.values() if v is not None]
    summary["avg"] = round(sum(valid) / len(valid), 4) if valid else None

    print("\n=== RAGAS SUMMARY (new chatbot) ===")
    for m in METRICS:
        s = summary[m]
        bar = ("█" * int((s or 0) * 20) + "░" * (20 - int((s or 0) * 20))) if s is not None else "N/A"
        print(f"  {METRIC_CONFIG[m][2]:<26} {bar} {s}")
    print(f"  {'AVG':<26} {summary['avg']}")
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(_REPO, "logs", f"ragas_new_{args.judge}_{ts}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump({"judge": f"{args.judge}:{args.model}", "n": len(data), "summary": summary, "results": results},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("report ->", out)


if __name__ == "__main__":
    main()
