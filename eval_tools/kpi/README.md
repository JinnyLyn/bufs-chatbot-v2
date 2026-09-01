# `eval_tools/kpi/` — KPI 자동 게이트 (사용법)

한 줄로 RAG 챗봇의 KPI를 측정하고 **GO / NO-GO**를 판정하는 도구.
"깨끗한 벤치마크 정답률"과 "실사용(오타·띄어쓰기·모호 입력) 정답률"의 **갭**까지 측정한다.

> 왜 만들었나 / 실측 결과(벤치 84% → 실사용 79%, 갭 4.7pp)는 [`FINDINGS.md`](FINDINGS.md) 참조.
> 채점기 통합 근거는 [`SCORER_EQUIVALENCE.md`](SCORER_EQUIVALENCE.md), 설계 합의는 `.omc/plans/kpi-eval-tool.md`.

## 빠른 시작

```bash
# 1) 백엔드 기동 (qwen3.5:9b 서빙하는 Ollama를 가리킴; h100-fast = nocompress)
OLLAMA_BASE_URL=http://<ollama-host>:11434 LLM_MODEL=qwen3.5:9b \
  LLM_NUM_CTX=16384 BASE_TOKEN_THRESHOLD=12000 LANGFUSE_ENABLED=false \
  python project/server.py            # :8000  (H100 배포 .env와 동일 config — MIGRATION_H100.md 3-2)

# 2) 게이트 한 줄
python -m eval_tools.kpi run --profile h100-fast --backend-url http://localhost:8000
```

출력 = `VERDICT: GO/NO-GO` + 가족별(정확도·거부·지연·RAGAS·검색) 판정 + 리포트 경로.

## 종료 코드 (CI에서 이걸로 막는다)

| 코드 | 뜻 |
|---|---|
| `0` | **GO** — 통과 (또는 advisory NO-GO) |
| `1` | **NO-GO** — 기준 미달 (floor/회귀) → 배포 막기 |
| `2` | **ERROR** — 못 잼 (백엔드 끊김·judge 없음 등). "미달"과 구분됨 |

## 서브커맨드

```bash
# run     : 측정 → 게이트 → 리포트
python -m eval_tools.kpi run --profile h100-fast --backend-url http://localhost:8000
python -m eval_tools.kpi run --profile h100-fast --from-predictions <dir>   # 오프라인 재채점(라이브 LLM 불필요)

# gate    : 이미 있는 예측/지표 덤프 재평가
# baseline-update : N=3 캡처로 베이스라인 시드 (+ --set-floors 로 floor 확정 & advisory→blocking)
python -m eval_tools.kpi baseline-update --profile h100-fast --from-predictions <dir> --temp 0
```

주요 플래그: `--testset <path>` / `--format qa`(외부 Q-A 데이터셋) / `--with-ragas` / `--require-ragas` / `--require-retrieval` / `--seed N` / `--real-from-predictions <dir>`(실사용 갭).

## 프로파일 (`kpi_profiles.yaml`)

운영점(머신+config)별로 임계값·베이스라인을 키잉. **정확도는 머신 무관에 가깝지만 동일하진 않으므로 config별로 분리한다.**
- `h100-fast` — 배포 config(nocompress + fast-refuse). **게이트 of record.**
- `4090-local` — 개발/사전점검(compress on).
- `local-cpu` — Phase 1 범위 밖(placeholder).

## advisory vs blocking

`h100-fast`의 floor(`contains_floor`/`strict_floor`)는 아직 **추측치**라 게이트가 **advisory**(보고만, 절대 exit 1 안 함).
H100에서 N=3 캡처 후 `baseline-update --set-floors`로 floor를 실측하면 `gating: advisory → blocking`으로 전환된다.

```bash
# H100 박스에서 원커맨드 (백엔드 떠 있는 상태): N=3 캡처 → floors 확정 → blocking 전환
./scripts/kpi-baseline-h100.sh
# 끝나면 kpi_profiles.yaml + baselines/h100-fast.json 커밋
```

## 실사용 갭 측정 (핵심 KPI)

```bash
# 변형셋 생성 (88문항 → 468, seed 결정적, 정답 보존)
python - <<'PY'
import json; from eval_tools.kpi.sources.perturb import load_answerable_parents, perturb_dataset
json.dump(list(perturb_dataset(load_answerable_parents())), open("perturbed.json","w"), ensure_ascii=False)
PY
# 실사용 run → 헤드라인 benchmark_real_gap_pp (깨끗 − 실사용)
python -m eval_tools.kpi run --profile h100-fast --backend-url http://localhost:8000 --testset perturbed.json
python -m eval_tools.kpi run --profile h100-fast --from-predictions <clean> --real-from-predictions <real>
```

## 결과물 위치

- 리포트(`report.md`/`report.json`/`predictions.json`) → **gitignored** `eval_tools/runs/<ts>-<profile>-<sha>/`
- 커밋되는 것: `data/combined88.json`(입력만), `kpi_profiles.yaml`, `baselines/<profile>.json`

## 테스트

```bash
pytest tests/kpi/ -m "not integration"   # 234개, 라이브 LLM 불필요(오프라인·CI 가능)
```
