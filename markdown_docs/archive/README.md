# archive/ — 색인 제외(은퇴) 문서 보관소

이 폴더의 `.md`는 챗봇 KB에 **색인되지 않는다** — 모든 색인 경로(`project/reindex.py`,
`DocumentManager`, health의 `kb_docs`)는 `markdown_docs/` 바로 아래 `*.md`만 비재귀로 읽는다.

- 은퇴: `git mv "markdown_docs/문서.md" markdown_docs/archive/` → 서버 중지 → `python project/reindex.py` → 서버 시작
- 복원: 반대로 꺼낸 뒤 동일하게 reindex

전체 절차·주의사항: [KB_MANAGEMENT.md](../../KB_MANAGEMENT.md)
