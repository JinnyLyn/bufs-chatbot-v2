# eval_tools — 평가/분석 하니스

세션 중 만든 일회성·재사용 평가 스크립트 모음. 백엔드(`localhost:8000`)와 Langfuse(`project/.env`의 키)를 사용.
주력 스크립트(`_eval_qa100`/`_ragas_eval`/`_answer_analysis`)는 **레포 내 골든셋**(`datasets/qa_dataset.json`)을 쓰므로 클론 후 재현 가능. repo 루트에서 `python eval_tools/<script>.py` 실행.

## 골든 데이터셋
- **`datasets/qa_dataset.json`** — 레포 내 정규 평가셋(100문항). 스키마: `id/question/gold_intent/gold_document/gold_chunk_id/expected_answer/must_include/must_not_include/difficulty/category`. 클론 후 바로 재현 가능(레포 밖 절대경로 의존 없음).
- **`qa_scorer.py`** — 임포트 가능한 순수 채점 모듈(네트워크 X). 데이터셋 로더·검증 + `must_include`/`must_not_include` 룰 채점 + intent/문서 recall 헬퍼. `tests/eval/`에서 단위 테스트로 보호. `pythonpath=["eval_tools"]`로 `import qa_scorer`.

## 재사용 (정기 회귀/평가)
- **`_eval_qa100.py`** ⭐ — **1순위** 룰기반 평가. `datasets/qa_dataset.json`(100문항)을 백엔드에 돌려 strict_pass/contains/violation + intent 정확도 + 검색 recall(gold_document) + 카테고리/난이도 분해. `--dry-run`(오프라인 검증) · `--n N`(서브셋) · `--base`. `logs/qa100_result.json` 출력.
- **`_eval_combined88.py`** — (레거시) bufs combined88(89문항) 룰기반. 레포 밖 `bufs-chatbot/...` 절대경로를 읽어 클론 환경에선 재현 불가 — 비교/이력용으로만 유지. contains/strict/refusal + duration_ms. `logs/combined88_new_result.json` 출력.
- **`_ragas_eval.py`** — RAGAS 5지표(LLM-judge). in-repo 골든셋 사용(`expected_answer`=reference). `--judge gemini|ollama --model … --n N --dataset PATH`. 생성=백엔드, judge=Gemini(REST) 또는 로컬 Ollama.
- **`_langfuse_analyze.py`** — Langfuse 트레이스 집계(지연분포·노드별·에러·툴호출 깊이).
- **`_answer_analysis.py`** — 정답/오답을 **검색실패 vs 생성실패**로 귀인. in-repo 골든셋의 `must_include` 토큰을 답변/컨텍스트(Langfuse) 양쪽에 대조.
- **`_check2020.py`** — 단일 회귀 질의("2020학번 졸업요건") 빠른 확인.

## 일회성 (특정 수정 검증, 보관용)
- `_compare_ba.py` / `_compare_h100.py` — before/after, local vs H100 비교(보정 채점 + 지연).
- `_rescore88.py` — 저장된 답변 오프라인 재채점(채점기 보정).
- `_verify_fixes.py` / `_test_fastrefuse.py` — 학사일정/졸업 수정, 빠른거부 포커스 검증.
- `_langfuse_drill.py` — 느린 트레이스 노드 타임라인 드릴다운.
- `_eval_runner.py` — 초기 eval_ko 서브셋 러너.
- `_md2docx.py` — `REPORT_*.md` → `.docx` 변환.

## 채점 메모
- **qa100(1순위)** — 명시적 `must_include`(전부 포함=strict_pass, 일부=contains) / `must_not_include`(하나라도 포함=violation) 룰 채점. 거부 휴리스틱이 없으므로 "불가"/"없습니다" 같은 단어가 정답에 있어도 거부로 오인하지 않음(false-refusal 버그 구조적 차단). 공백 무시 매칭. `tests/eval/`가 회귀 보호.
- **combined88(레거시)** — 숫자/날짜/등급 자동 추출 + **자릿수 경계 매칭**(`6`≠`16`), 날짜 표면형 변환, 24/12시 등가, "불가능" 단어 거부 오인 보정.

결과 파일은 `logs/`.
