# archive/ — 색인 제외(은퇴) 문서 보관소

이 폴더의 `.md`는 챗봇 KB에 **색인되지 않는다** — 모든 색인 경로(`project/reindex.py`,
`DocumentManager`, health의 `kb_docs`)는 `markdown_docs/` 바로 아래 `*.md`만 비재귀로 읽는다.

- 은퇴/복원은 직접 옮기지 말고 `scripts/doc_sync.sh` 로: 원본(또는 빈 마커)을
  `pdfs/archive/` ↔ `pdfs/` 사이에서 옮긴 뒤 `scripts/doc_sync.sh apply --restart`
  (md 이동 + reindex + 서버 재시작까지 처리)
- 수동으로 할 때만: `git mv` 로 이동 → 서버 중지 → `python project/reindex.py` → 서버 시작

전체 절차·주의사항: [KB_MANAGEMENT.md](../../KB_MANAGEMENT.md)
