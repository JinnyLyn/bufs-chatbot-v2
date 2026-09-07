# reports/ — 보고서 모음

행정 담당자·팀이 읽는 보고서. `.md`가 원본이고 `.docx`는 같은 내용의 워드 판이다 (장애 대응 보고서만 PDF 최종본).

| 파일 | 내용 |
|---|---|
| `REPORT_결과.md` / `.docx` | 성능 점검 보고서 (정확도·RAGAS·부하 시험·타 대학 비교) |
| `REPORT_vs_BUFS.md` / `.docx` | 신규 agentic-RAG vs 기존 BUFS-CHATBOT 비교 |
| `CamChat-장애대응.pdf` | 장애 대응 및 운영 계획 (2026-09-04 기준, 최종본은 PDF만 관리) |
| `emergency_plan.md` | 장애 대응 보고서의 바탕이 된 팀 플랜 메모 (원본) |

## 다시 만들기

- `REPORT_결과`: 내용이 `make_report.py`의 `SECTIONS`에 있다. 수정 후 `python reports/make_report.py` → md·docx 동시 생성.
- `REPORT_vs_BUFS`: `.md`를 직접 고친 뒤 `python eval_tools/_md2docx.py reports/REPORT_vs_BUFS.md` 로 `.docx`를 다시 뽑는다.
- `CamChat-장애대응.pdf`: 레포 밖에서 편집한 최종본이다. 바뀌면 PDF를 통째로 교체한다.

평가 원자료(`logs/`, `eval_tools/runs/`)는 커밋하지 않는다. 보고서의 수치 출처는 각 문서 끝의 측정 조건을 본다.
