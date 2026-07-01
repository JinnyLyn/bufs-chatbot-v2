# QA / Evaluation 가이드 — 데이터셋 · 오답 분석 · KPI

QA/평가 엔지니어의 세 가지 산출물을 한 곳에 정리한다. 전부 **순수·오프라인**
(라이브 LLM/Qdrant/네트워크 불필요)이라 클론 후 바로 재현되고 CI에서 돈다.

| 산출물 | 위치 | 무엇 |
|---|---|---|
| ① 질문 데이터셋 | `datasets/qa_dataset.json`(상세 100), `datasets/qa_short_queries.json`(짧은/구어체 38) | 정답률 측정용 골든셋 |
| ② 오답 분석 | `kpi/error_analysis.py` | 오답을 7종으로 자동 분류 |
| ③ KPI | `kpi/metrics.py` | Accuracy·Precision·Recall·F1·Faithfulness |

---

## ① 질문 데이터셋

두 종류를 함께 굴려 **"깨끗한 문장 정답률"과 "실사용 짧은 질의 정답률"의 갭**을 본다.

- **`qa_dataset.json`** — 상세 시나리오형 100문항(기존 정규셋).
- **`qa_short_queries.json`** — 학생이 실제로 치는 **짧고 구어체·축약형** 질의 38문항
  (`수강신청 언제야?`, `복전 신청기간`, `복전 언제 신청`, `군휴학`, `휴학연장`, `전과 신청`,
  `계절학기 뭐 열려?` …). 스키마는 `qa_dataset.json`과 동일해 `qa_scorer.load_dataset`이
  그대로 읽는다. 추가 필드:
  - `query_style`: `"short_colloquial"`
  - `paraphrase_of`: 정답 근거를 가져온 `qa_dataset.json` 원본 id (검증된 gold 재사용)
  - `answerable`: bool. 범위 밖(학식/셔틀 등) 질의는 `false` — **거부해야 정답**.
    → 답/거부 분류 기반 Precision/Recall 측정에 쓰인다.

  각 답변형 레코드는 검증된 원본의 `expected_answer`/`must_not_include`를 재사용하므로
  **gold 자기일관성**(정답이 자기 금지어를 어기지 않음)이 보장된다.
  `tests/eval/test_qa_short_queries.py`가 강제한다.

```bash
# 데이터셋 로드·검증 (순수)
python -c "import qa_scorer; print(len(qa_scorer.load_dataset('eval_tools/datasets/qa_short_queries.json')))"
```

## ② 오답 분석 (7분류)

1000문제 → 800정답 / 200오답에서, **200오답을 7종으로 자동 분류**한다.
`_answer_analysis.py`의 2분류(검색실패 vs 생성실패)를 7버킷으로 확장한 것.

| 분류 | 코드 | 발동 신호(휴리스틱) |
|---|---|---|
| 검색 실패 | `RETRIEVAL_FAIL` | 질의와 겹치는 청크는 회수됐는데 정답 근거가 top-k에 없음(recall/랭킹) |
| 문서 없음 | `NO_DOCUMENT` | 검색 결과 0건 |
| Chunk 문제 | `CHUNK` | 정답 문서는 회수됐으나 그 청크에 정답 문장이 안 잘림 |
| Embedding 문제 | `EMBEDDING` | 회수 청크의 질의 어휘 겹침이 낮음(임베딩 이웃 어긋남) |
| 질문 애매함 | `AMBIGUOUS` | 질의가 너무 짧고(≤2토큰) 검색이 여러 문서로 흩어짐 |
| Prompt 실패 | `PROMPT` | 근거는 컨텍스트에 있는데 답이 거부/공란 |
| LLM Hallucination | `HALLUCINATION` | 근거가 있는데(혹은 unanswerable인데) 엉뚱한 답을 지어냄 |

> **휴리스틱 triage이지 정답이 아니다.** 각 결과의 `reason`에 어떤 신호가 발동했는지
> 남으므로 사람이 스팟체크로 뒤집을 수 있다. 임계값은
> `AMBIGUOUS_MAX_QTOKENS`/`AMBIGUOUS_MIN_DISPERSION`/`EMBEDDING_MAX_OVERLAP` 상수.

```bash
# 예측 덤프(리스트 또는 {"results":[...]}: {id, answer, retrieved/results}) 를 채점·분류
python -m eval_tools.kpi.error_analysis \
  --dataset eval_tools/datasets/qa_short_queries.json --predictions preds.json
python -m eval_tools.kpi.error_analysis --dataset ... --predictions ... --json  # 기계용
```

예시 출력:

```
# 오답 분석 (Error Analysis)
- 총 문항: 38
- 정답: 8  /  오답: 30  (Accuracy 21.1%)
| 분류 | 코드 | 개수 | 오답 중 비율 |
| Prompt 실패 | PROMPT | 10 | 33.3% |
| 문서 없음 | NO_DOCUMENT | 9 | 30.0% |
| LLM Hallucination | HALLUCINATION | 8 | 26.7% |
| Chunk 문제 | CHUNK | 3 | 10.0% |
```

## ③ KPI

한 번의 채점 런에서 다섯 지표를 낸다. **답/거부 결정을 이진분류**로 보고 P/R/F1을 정의한다.

```
gave = 답이 공란도 거부도 아님(실제 내용을 답함, gold 인지 — 부정어 오인 방지)
correct = 정답 사실 포함 & 금지어 없음 / (거부해야 할 땐 거부)

TP  답했고 맞음      FP  답했는데 틀림·거부했어야 하는데 답함
FN  답할 수 있었는데 거부/공란   TN  거부해야 할 걸 제대로 거부

Accuracy=(TP+TN)/N   Precision=TP/(TP+FP)   Recall=TP/(TP+FN)   F1=2PR/(P+R)
Faithfulness = 답의 주장 중 회수 컨텍스트로 뒷받침되는 비율(어휘 근거 proxy)
```

- **False-refusal 주의**: 정답에 "불가"/"없습니다" 같은 부정어가 들어 있어도 gold 사실이
  있으면 **거부로 오인하지 않는다**(`scorer.py`의 D3 교훈). 그 덕에 `metrics.Accuracy`는
  `error_analysis`의 정답률과 **정확히 일치**한다(교차검증 테스트가 강제).
- **Faithfulness**는 결정적 **어휘 근거 proxy**다. LLM-judge 버전(RAGAS 5지표)은
  `_ragas_eval.py`가 담당 — judge 모델이 있을 때 쓰고, 이건 빠른 오프라인 게이트로 쓴다.
- 보너스로 컨텍스트가 있으면 **Retrieval recall/precision**(근거 회수율/답변 시 근거 확보율)도 낸다.

```bash
python -m eval_tools.kpi.metrics \
  --dataset eval_tools/datasets/qa_short_queries.json --predictions preds.json
python -m eval_tools.kpi.metrics --dataset ... --predictions ... --json
```

## 예측 덤프 포맷

두 도구 모두 `id`로 골든셋과 left-join한다. 예측이 없는 id는 오답 처리(누락도 오답).

```json
[
  {"id": 2001, "answer": "모델 답변…",
   "results": [{"text": "회수 청크 본문", "source": "문서명", "score": 0.87}]}
]
```

- `answer` | `model_answer` | `prediction` 중 아무 키나 인식.
- 회수 컨텍스트: `retrieved` | `results` | `retrieved_docs` | `contexts`; 각 항목은
  `text/content/page_content` + `doc/source`(중첩 `metadata`도 허용) + `score`.
- `correct`(bool)를 넣어 두면 룰 채점 대신 그 판정을 그대로 쓴다(외부 채점기 연동).

> **정확도 판정 주의**: `correct`를 안 주면 내장 폴백이 답변형은 `must_include` **전부 포함 &
> 금지어 없음**, 거부형은 거부 여부로 판정한다. `qa_dataset.json`의 `must_include`는 느슨한
> 키워드라(README 참고) 이 폴백이 **엄격**하게 나올 수 있으니, 실측 정답률은 RAGAS(`_ragas_eval.py`)
> 나 KPI 채점기의 판정을 `correct`로 넘겨 쓰는 것을 권장한다. `qa_short_queries.json`은
> `must_include`를 정답에 실제로 들어가게 큐레이션해 폴백만으로도 안정적이다.

## 테스트

```bash
pytest tests/kpi/test_error_analysis.py tests/kpi/test_metrics.py \
       tests/eval/test_qa_short_queries.py -q     # 순수·오프라인
```
