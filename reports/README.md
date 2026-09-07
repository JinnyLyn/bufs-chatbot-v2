# reports/ — 보고서 모음

행정 담당자·팀이 읽는 보고서. `.md`가 원본이고 `.docx`는 같은 내용의 워드 판이다 (장애 대응 보고서만 PDF 최종본).

| 파일 | 내용 |
|---|---|
| `REPORT_결과_0908.md` / `.docx` | **최신 통합 보고서** = 9/2 성능 점검(제출본 그대로) + 장애 대응 계획·구현 현황·모의 시험 결과 (2026-09-08) |
| `REPORT_결과_0902.docx` | 9/2 제출본(성능 점검). 손으로 다듬은 최종 문장이라 이 파일이 원본 |
| `REPORT_결과.md` | 9/2 성능 점검의 md 판(생성물, 제출본과 문장 일부 다름) |
| `REPORT_vs_BUFS.md` / `.docx` | 신규 agentic-RAG vs 기존 BUFS-CHATBOT 비교 |
| `REPORT_장애대응.docx` / `CamChat-장애대응.pdf` | 9/4 장애 대응 계획 제출본(docx·PDF). 구현 현황은 `REPORT_결과_0908` 2편에 |
| `emergency_plan.md` | 장애 대응 보고서의 바탕이 된 팀 플랜 메모 (원본) |

## 다시 만들기

- `REPORT_결과_0908`: `python reports/make_report_0908.py` — `REPORT_결과_0902.docx` 를 그대로 복사한 뒤 2편(장애 대응)을 붙인다. 2편 내용은 그 스크립트의 `SECTIONS`.
- `REPORT_결과`(9/2 판): 내용이 `make_report.py`의 `SECTIONS`에 있다. `python reports/make_report.py` → md·docx 동시 생성.
- `REPORT_vs_BUFS`: `.md`를 직접 고친 뒤 `python eval_tools/_md2docx.py reports/REPORT_vs_BUFS.md` 로 `.docx`를 다시 뽑는다.
- `CamChat-장애대응.pdf`: 레포 밖에서 편집한 최종본이다. 바뀌면 PDF를 통째로 교체한다.

평가 원자료(`logs/`, `eval_tools/runs/`)는 커밋하지 않는다. 보고서의 수치 출처는 각 문서 끝의 측정 조건을 본다.
