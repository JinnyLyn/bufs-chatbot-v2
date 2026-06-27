# KPI Gate — First Live Findings (2026-06-24)

First end-to-end live run of `eval_tools/kpi/` against a real backend, dogfooding
the tool. **Measurement box:** jin's 4090 Ollama (`100.91.6.58:11434`), model
`qwen3.5:9b`, **h100-fast config** (nocompress `BASE_TOKEN_THRESHOLD=12000`,
`num_ctx=8192`, fast-refuse on, Langfuse off). `temp=0`, `seed=42`.

> Caveat: this is the 4090, **not** the H100 gate-of-record. Accuracy is
> ~machine-agnostic but not identical (see below); the h100-fast floors stay
> **advisory** until measured from a real H100 N=3 capture. Langfuse trace-mining
> needs `LANGFUSE_HOST` set on the production box.

## 1. Clean benchmark (combined88), N=3

3 runs at `temp=0, seed=42` were **byte-identical** → within-box generation is
fully deterministic, **flaky set = 0**. So a single run is trustworthy *on a
fixed box*.

| metric | 4090 (this) | H100 pinned snapshot | Δ |
|---|---|---|---|
| contains | **84.0%** (68/81) | 85.2% (69/81) | −1.2pp |
| strict | **72.8%** (59/81) | 66.7% (54/81) | **+6.1pp** |
| out-of-doc refusal | 100% (8/8) | 100% (8/8) | — |
| latency p95 / max | 6.6s / 8.6s | ~28.6s max (REPORT) | faster |

**The strict +6pp is reproducible, not noise** (3/3 identical) → a real
cross-box difference (quantization or the H100 snapshot's config), which is
exactly why baselines are keyed per config + box, not per machine name.

## 2. Headline: benchmark ↔ real-usage gap

Real users type messy queries; combined88 is clean. The perturbation suite
(`sources/perturb.py`, 81 answerable → 468 seeded children, ground_truth
preserved) measures the gap.

**`benchmark_real_gap_pp = 4.7`**  (clean **84.0%** → real-usage **79.3%**, 371/468; strict 72.8% → 66.2%).

The gap is **not uniform** — by perturbation type:

| perturbation | contains | gap vs clean |
|---|---|---|
| **truncation** (incomplete / trailing-off query) | 74.1% | **−9.9pp** ← worst |
| **spacing** (붙여쓰기, no spaces) | 77.8% | **−6.2pp** ← stresses the kiwi tokenizer |
| paraphrase | 80.2% | −3.7pp |
| typo (jamo / keyboard) | 80.2% | −3.7pp |
| codeswitch (KO→EN term) | 81.0% | −3.0pp |
| honorific (반말) | 82.7% | −1.2pp ← negligible |

**Takeaways**
- Incomplete queries (**truncation**) and **no-spacing** input cost the most —
  directly confirming the original motivation (ambiguous input + tokenizer are
  the real-world failure modes; register/politeness is not).
- "84% on the benchmark" is really **~79% under realistic input**; the gate now
  measures and can block on that gap (`max_gap_pp`, advisory until floors set).

## 3. Reproduce

```bash
# 1) backend pointed at any Ollama serving qwen3.5:9b (h100-fast config)
OLLAMA_BASE_URL=http://<host>:11434 LLM_MODEL=qwen3.5:9b LLM_NUM_CTX=8192 \
  BASE_TOKEN_THRESHOLD=12000 LANGFUSE_ENABLED=false python project/server.py

# 2) clean N=3 → collect the three runs/.../predictions.json into <cap>/ → seed baseline
for i in 1 2 3; do python -m eval_tools.kpi run --profile h100-fast \
  --backend-url http://localhost:8000 --seed 42; done
python -m eval_tools.kpi baseline-update --profile h100-fast --from-predictions <cap> --temp 0

# 3) real-usage gap
python - <<'PY'
import json; from eval_tools.kpi.sources.perturb import load_answerable_parents, perturb_dataset
json.dump(list(perturb_dataset(load_answerable_parents())), open("perturbed.json","w"), ensure_ascii=False)
PY
python -m eval_tools.kpi run --profile h100-fast --backend-url http://localhost:8000 \
  --testset perturbed.json --seed 42
python -m eval_tools.kpi run --profile h100-fast \
  --from-predictions <cap> --real-from-predictions <real runs/.../predictions.json>
```

Raw per-run reports land in gitignored `eval_tools/runs/<ts>-<profile>-<sha>/`.
