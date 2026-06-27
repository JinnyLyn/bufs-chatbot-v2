# eval_tools — 평가/분석 하니스

세션 중 만든 일회성·재사용 평가 스크립트 모음. 백엔드(`localhost:8000`)와 Langfuse(`project/.env`의 키)를 사용.
대부분 절대경로를 쓰므로 어디서 실행해도 동작하지만, repo 루트에서 `python eval_tools/<script>.py` 권장.

## kpi/ — 자동 KPI 게이트
`eval_tools/kpi/`는 룰기반 정답률·거부율·지연을 **프로파일 임계값**과 비교해 GO/NO-GO/ERROR
판정과 **프로세스 종료코드**(0=GO, 1=NO-GO, 2=ERROR)를 내는 자동 게이트다. 순수·오프라인
경로(`--from-predictions`)는 네트워크/Ollama/Qdrant 의존이 없다.

```bash
# 저장된 예측 덤프(N개)를 채점→게이트→리포트 (한 줄 실행)
python -m eval_tools.kpi run --profile h100-fast --from-predictions logs/

# 기존 덤프/메트릭 재평가만 (리포트 미작성)
python -m eval_tools.kpi gate --profile h100-fast --from-predictions logs/

# N=3 캡처로 베이스라인 갱신 + 측정된 바닥값을 yaml에 기록하고 advisory→blocking 전환
python -m eval_tools.kpi baseline-update --profile h100-fast --from-predictions logs/ --set-floors
```

- **프로파일**(`eval_tools/kpi_profiles.yaml`): `h100-fast`(배포 구성, **게이트 기준**),
  `4090-local`(jin 개발 사전점검, 항상 advisory), `local-cpu`(Phase 1 범위 외).
- **advisory-until-measured**: FLAG(미측정) 바닥값이 남아 있는 동안 `h100-fast`는 **advisory**라
  NO-GO를 계산·보고하되 **종료코드 1로 막지 않는다**. H100 N=3 덤프로
  `baseline-update --set-floors`를 돌려 바닥값을 측정하면 자동으로 `gating: blocking`으로 바뀐다.
- **리포트**: `eval_tools/runs/<ts>-<profile>-<shortsha>/`에 `report.md`/`report.json`/
  `predictions.json` 출력(이 폴더는 `.gitignore` 처리, 커밋 안 함). 리포트에는 실행 컨텍스트
  STAMP가 그대로 박히고, 가족별 판정·사유와 목표대비 격차(target_contains − current)가 표시된다.
- **real-usage suite**: 깨끗한 combined88과 실사용(perturb/langfuse/qa) 예측을 함께 채점하면
  헤드라인 **benchmark↔real gap(pp)** 이 리포트에 나온다
  (`run --from-predictions <clean> --real-from-predictions <real>`).

## 재사용 (정기 회귀/평가)
- **`_eval_combined88.py`** — bufs combined88(89문항) 룰기반 평가. contains/strict/refusal + duration_ms. `logs/combined88_new_result.json` 출력.
- **`_ragas_eval.py`** — RAGAS 5지표(LLM-judge). `--judge gemini|ollama --model … --n N`. 생성=백엔드, judge=Gemini(REST) 또는 로컬 Ollama.
- **`_langfuse_analyze.py`** — Langfuse 트레이스 집계(지연분포·노드별·에러·툴호출 깊이).
- **`_answer_analysis.py`** — 정답/오답을 **검색실패 vs 생성실패**로 귀인(Langfuse 컨텍스트 대조).
- **`_check2020.py`** — 단일 회귀 질의("2020학번 졸업요건") 빠른 확인.

## 일회성 (특정 수정 검증, 보관용)
- `_compare_ba.py` / `_compare_h100.py` — before/after, local vs H100 비교(보정 채점 + 지연).
- `_rescore88.py` — 저장된 답변 오프라인 재채점(채점기 보정).
- `_verify_fixes.py` / `_test_fastrefuse.py` — 학사일정/졸업 수정, 빠른거부 포커스 검증.
- `_langfuse_drill.py` — 느린 트레이스 노드 타임라인 드릴다운.
- `_eval_runner.py` — 초기 eval_ko 서브셋 러너.
- `_md2docx.py` — `REPORT_*.md` → `.docx` 변환.

## 채점 메모
숫자/날짜/등급을 자동 추출해 **자릿수 경계 매칭**(`6`≠`16`), 날짜 표면형 변환, 24/12시 등가, "불가능" 같은 단어로 정답을 거부로 오인하지 않도록 보정. 결과 파일은 `logs/`.
