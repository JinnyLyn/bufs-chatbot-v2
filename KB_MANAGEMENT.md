# KB 문서 관리 가이드 — 추가·제거(은퇴)·복원

학기/연도가 바뀔 때 챗봇이 참조하는 문서 세트를 안전하게 넣고 빼는 절차.
(설계 배경: 2026-09-01, "1학기 문서 노이즈" 논의에서 확정한 운영 방식)

## 한눈에 보는 구조

| 경로 | 역할 |
|------|------|
| `pdfs/` | 원본 PDF 보관소. **색인이 직접 읽지 않는다** — `ingest.py` 실행 때 인자로 준 경로에서 읽는 최초 변환 재료일 뿐. |
| `markdown_docs/` | **KB의 실제 소스.** 문서 1개 = `.md` 1개 (Docling 변환본). 커밋 대상. 여기 있는 파일이 곧 색인되는 문서 세트다. |
| `markdown_docs/archive/` | 색인에서 제외한 은퇴 문서 보관소. 모든 색인 경로(`reindex.py`, `DocumentManager`, health의 `kb_docs` 카운트)가 `markdown_docs/` 바로 아래 `*.md`만 비재귀로 읽으므로, **여기로 옮기면 KB에서 빠진다.** 코드 변경 불필요. |
| `qdrant_db/` + `parent_store/` | `markdown_docs/`에서 생성되는 산출물(child 벡터 색인 + parent 원문 청크). 수동 편집 금지 — 항상 `reindex.py`로 통째 재생성. |

파이프라인: PDF → (`project/ingest.py`, Docling 변환) → `markdown_docs/*.md` → (parent/child 청킹) → Qdrant(`document_child_chunks`, dense+sparse 하이브리드) + `parent_store/`.

## 공통 주의 (모든 절차 전에)

- **백엔드를 먼저 내려라.** Qdrant는 embedded(단일 프로세스 락)라 서버가 살아있으면 ingest/reindex가 DB를 못 연다.
  - H100 운영 서버: `systemctl --user stop agentic-rag` / 끝나면 `systemctl --user start agentic-rag`
    (`start-all.sh` 재실행으로 올리지 말 것 — 살아있는 포트 위에 재실행하면 pidfile이 유실된다.)
- 파이썬은 프로젝트 venv로: `~/camchat/.venv/bin/python` (conda python은 fastapi 등이 없다).

## 절차

### 문서 추가 (새 학기 학사안내 등)

```bash
cp 새문서.pdf pdfs/                          # 1. 원본 보관
systemctl --user stop agentic-rag            # 2. 서버 중지
.venv/bin/python project/ingest.py "pdfs/새문서.pdf"   # 3. 변환+청킹+색인
systemctl --user start agentic-rag           # 4. 서버 시작
```

생성된 `markdown_docs/새문서.md`를 커밋한다 (원본 PDF도 pdfs/에 함께 커밋).

### 문서 제거 (은퇴)

```bash
git mv "markdown_docs/문서이름.md" markdown_docs/archive/   # 1. 이동
systemctl --user stop agentic-rag                           # 2. 서버 중지
.venv/bin/python project/reindex.py                         # 3. 클린 재빌드
systemctl --user start agentic-rag                          # 4. 서버 시작
```

`reindex.py`는 `qdrant_db/`를 물리 삭제 후 `markdown_docs/` 바로 아래 `*.md`만으로 재구축한다
(md 삭제만으로는 이미 임베딩된 청크가 남기 때문에 재빌드가 필수).

### 복원

archive에서 다시 꺼내고 재빌드:

```bash
git mv "markdown_docs/archive/문서이름.md" markdown_docs/
# 이후 위와 동일: 서버 중지 → reindex.py → 서버 시작
```

### 변경 후 확인

1. `reindex.py` 출력의 `Indexed N unique doc(s)`가 기대 문서 수와 맞는지.
2. `bash scripts/healthcheck.sh` — `kb_docs` 수가 `markdown_docs/*.md` 수와 일치하는지.
3. 문서 세트 변경은 검색·답변에 영향을 주므로 `python eval_tools/_eval_qa100.py`로 정답률 회귀 확인
   (PR 템플릿의 "정답률 결과" 칸).

## 학기·연도가 바뀔 때 무엇을 해야 하나

- **같은 학년도 안에서 1↔2학기 전환: 문서를 뺄 필요 없다.**
  `SEMESTER_FILTER_ENABLED=true`(운영 기본, `project/.env`)가 오늘 날짜 기준 학기를 판단해
  다른 학기 문서 청크를 검색 순위에서 뒤로 밀어낸다(demote — 삭제 아님, `rag_agent/semester.py`).
  지난 학기 소급 질문("1학기 성적 정정 언제였지")도 계속 답할 수 있다.
  새 학기 학사안내가 나오면 **"문서 추가" 절차로 넣기만** 하면 된다.
- **학년도 롤오버(예: 2027학년도 문서 도입 시): 이때가 archive를 쓰는 시점.**
  학기 필터는 1/2학기만 구분하고 연도는 구분하지 않으므로, 2027-1 문서가 들어오면
  2026-1 문서가 같은 "1학기"로 취급되어 필터가 못 거르는 노이즈가 된다.
  전 학년도 학기별 문서들을 `markdown_docs/archive/`로 이동하고 reindex한다.
- `KB_EXCLUDE_SOURCES`(env, stem 매칭)는 파일 이동 없이 색인에서 빼는 **보조 수단**
  (기본값: 국가근로장학금 근로기관 안내자료 — #108). 실험·임시 제외에 쓰고,
  영구 은퇴는 archive 이동이 정석. 어느 쪽이든 반영에는 reindex가 필요하다.

## 하지 말 것

- `markdown_docs/`에 KB용이 아닌 `.md`를 두지 말 것 — reindex가 전부 색인해 챗봇 KB에 들어간다.
- 서버가 살아있는 채로 ingest/reindex 금지 (Qdrant 단일 프로세스 락).
- `qdrant_db/`·`parent_store/` 수동 편집 금지 — 항상 reindex로 재생성.
