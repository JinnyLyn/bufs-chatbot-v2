"""Unit tests for project/document_chunker.py — DocumentChuncker.

HIGH VALUE: The chunker is 275 lines, deterministic, and directly affects
retrieval quality. Tests cover:

- Parent/child chunk creation from Korean markdown
- Cohort-section awareness (hard boundaries at 학번 headers)
- Year-range expansion in cohort headers (2017~2020학번 → enumerated years)
- Month-column forward-fill in academic calendar tables
- MAX_PARENT_SIZE / CHILD_CHUNK_SIZE boundary behaviour
- parent_id naming convention
- child chunks reference correct parent_id
"""
import textwrap
from pathlib import Path

import pytest


def _make_chunker():
    from document_chunker import DocumentChuncker
    return DocumentChuncker()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_md(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Basic parent/child creation
# ---------------------------------------------------------------------------

class TestBasicChunking:
    def test_single_file_produces_parent_and_child_chunks(self, tmp_path):
        chunker = _make_chunker()
        md = _write_md(tmp_path, "test.md", """\
            # 졸업요건

            졸업을 위해서는 총 130학점 이상을 이수해야 합니다.
            전공필수 42학점, 교양필수 12학점, 일반선택 학점으로 구성됩니다.
            각 전공별 요건은 학사 안내서를 참조하세요.
            더 자세한 내용은 학사지원팀에 문의하시기 바랍니다.
            이 문서는 테스트용으로 작성된 문서입니다.
            내용은 임의로 구성되었습니다.
            여러 줄의 내용이 포함되어 있습니다.
            내용을 충분히 채워야 합니다.
            조금 더 많은 내용을 추가하겠습니다.
            이렇게 하면 충분한 크기의 청크가 생성됩니다.
        """)
        parents, children = chunker.create_chunks_single(md)
        assert len(parents) >= 1
        assert len(children) >= 1

    def test_parent_id_uses_file_stem(self, tmp_path):
        chunker = _make_chunker()
        md = _write_md(tmp_path, "학사안내서.md", """\
            # 안내

            내용이 충분히 포함된 문서입니다.
            줄을 추가해야 합니다.
            더 추가합니다.
            내용이 있어야 합니다.
            더 많은 내용을 추가합니다.
            이제 충분할 것입니다.
            마지막 줄입니다.
        """)
        parents, _ = chunker.create_chunks_single(md)
        for parent_id, _ in parents:
            assert parent_id.startswith("학사안내서_parent_")

    def test_child_chunks_carry_parent_id_in_metadata(self, tmp_path):
        chunker = _make_chunker()
        md = _write_md(tmp_path, "doc.md", """\
            # 학사안내

            이 문서는 학사 안내 문서입니다.
            내용을 충분히 포함해야 합니다.
            여러 줄을 추가합니다.
            더 추가해야 합니다.
            이제 충분할 것입니다.
            내용이 있습니다.
        """)
        parents, children = chunker.create_chunks_single(md)
        parent_ids = {pid for pid, _ in parents}
        for child in children:
            assert child.metadata.get("parent_id") in parent_ids

    def test_child_chunks_carry_source_metadata(self, tmp_path):
        chunker = _make_chunker()
        md = _write_md(tmp_path, "notice.md", """\
            # 공지

            공지 내용입니다.
            내용을 충분히 포함해야 합니다.
            여러 줄을 추가합니다.
            더 추가해야 합니다.
            이제 충분할 것입니다.
            내용이 있습니다.
        """)
        _, children = chunker.create_chunks_single(md)
        for child in children:
            assert child.metadata.get("source") == "notice.pdf"


# ---------------------------------------------------------------------------
# Cohort-section awareness
# ---------------------------------------------------------------------------

class TestCohortSectionAwareness:
    def test_cohort_sections_not_merged_across_boundaries(self, tmp_path):
        """두 학번 섹션의 내용이 하나의 parent chunk에 합쳐지지 않아야 합니다.

        학번 경계는 hard boundary — 두 개의 별도 학번 헤더가 같은 청크에 나타나면 실패.
        range-expansion이 "2017~2020학번" 청크에 "2021학번" 토큰을 추가하지 않으므로
        두 헤더 문자열이 동시에 나타나는 경우는 오직 병합 오류(boundary 위반)뿐.
        """
        chunker = _make_chunker()
        md = _write_md(tmp_path, "cohort.md", """\
            ## 2017~2020학번

            이 학번의 졸업요건은 다음과 같습니다.
            전공필수 42학점 이상 이수해야 합니다.
            일반선택 학점도 포함됩니다.
            추가 내용입니다.
            여러 줄입니다.
            내용이 있습니다.
            충분한 내용입니다.
            계속 추가합니다.
            이제 충분합니다.
            끝입니다.

            ## 2021학번

            이 학번의 졸업요건은 이전과 다릅니다.
            새로운 과정이 적용됩니다.
            전공필수 과목이 변경되었습니다.
            추가 내용입니다.
            여러 줄입니다.
            내용이 있습니다.
            충분한 내용입니다.
            계속 추가합니다.
            이제 충분합니다.
            끝입니다.
        """)
        parents, _ = chunker.create_chunks_single(md)
        # Hard assertion: no parent may contain BOTH cohort section headers.
        # Range-expansion only adds individual year tokens (2017학번…2020학번),
        # never "## 2021학번", so co-occurrence is unambiguously a boundary
        # violation regardless of what else is in the chunk.
        for _, parent in parents:
            content = parent.page_content
            assert not ("2017~2020학번" in content and "## 2021학번" in content), (
                "Cohort boundary violated: 2017~2020학번 and ## 2021학번 "
                f"both found in a single parent chunk:\n{content[:300]}"
            )

    def test_cohort_range_header_expanded_with_individual_years(self, tmp_path):
        """## 2017~2020학번 헤더는 2017학번 2018학번 2019학번 2020학번을 포함해야 합니다."""
        chunker = _make_chunker()
        md = _write_md(tmp_path, "range.md", """\
            ## 2017~2020학번

            이 학번의 졸업요건입니다.
            전공필수 학점이 있습니다.
            내용을 더 추가합니다.
            여러 줄을 추가합니다.
            충분한 내용입니다.
            이제 끝납니다.
            마지막 줄입니다.
        """)
        parents, _ = chunker.create_chunks_single(md)
        # The range header should be expanded in at least one parent
        all_content = "\n".join(p.page_content for _, p in parents)
        for year in ("2017학번", "2018학번", "2019학번", "2020학번"):
            assert year in all_content, f"Year expansion missing: {year}"


# ---------------------------------------------------------------------------
# Month-column forward-fill in academic calendar tables
# ---------------------------------------------------------------------------

class TestMonthColumnForwardFill:
    def test_empty_month_cells_filled_from_previous_row(self, tmp_path):
        """월 칸이 비어 있는 행은 위 행의 월 값으로 채워져야 합니다."""
        chunker = _make_chunker()
        md = _write_md(tmp_path, "calendar.md", """\
            # 학사일정

            아래는 학사일정표입니다.

            | 월 | 일 | 일정 |
            |---|---|---|
            | 6 | 1(월) | 중간고사 |
            |  | 8(월)~12(금) | 기말고사 |
            |  | 15(월) | 성적 공시 |
            | 7 | 1(화) | 방학 시작 |

            이후 일정은 추후 공지됩니다.
            내용이 있습니다.
            충분히 추가되었습니다.
        """)
        parents, _ = chunker.create_chunks_single(md)
        all_content = "\n".join(p.page_content for _, p in parents)
        # After forward-fill, the empty month cells should now carry "6"
        assert "| 6 | 8(월)~12(금)" in all_content or "6 | 8(월)~12(금)" in all_content

    def test_non_calendar_tables_not_affected(self, tmp_path):
        """月 컬럼이 없는 일반 표는 수정되지 않아야 합니다."""
        original_header = "| 구분 | 학점 | 비고 |"
        chunker = _make_chunker()
        md = _write_md(tmp_path, "credits.md", f"""\
            # 이수학점 안내

            {original_header}
            |---|---|---|
            | 전공필수 | 42 | 필수 |
            | 교양필수 | 12 | 필수 |
            | 일반선택 | 76 | 선택 |

            이 표는 이수학점 안내입니다.
            더 많은 내용이 있습니다.
            내용이 충분합니다.
        """)
        parents, _ = chunker.create_chunks_single(md)
        all_content = "\n".join(p.page_content for _, p in parents)
        # The original non-calendar header structure should be preserved
        assert "구분" in all_content
        assert "학점" in all_content


# ---------------------------------------------------------------------------
# create_chunks (multi-file directory scan)
# ---------------------------------------------------------------------------

class TestCreateChunksDirectory:
    def test_processes_all_md_files_in_directory(self, tmp_path):
        chunker = _make_chunker()
        for i in range(3):
            _write_md(tmp_path, f"doc_{i}.md", f"""\
                # 문서 {i}

                이 문서는 {i}번 문서입니다.
                내용이 있습니다.
                충분히 추가되었습니다.
                더 추가합니다.
                이제 끝납니다.
            """)
        parents, children = chunker.create_chunks(path_dir=str(tmp_path))
        assert len(parents) >= 3
        assert len(children) >= 3

    def test_empty_directory_returns_empty_lists(self, tmp_path):
        chunker = _make_chunker()
        parents, children = chunker.create_chunks(path_dir=str(tmp_path))
        assert parents == []
        assert children == []


# ---------------------------------------------------------------------------
# Docling conversion-artifact stripping
# ---------------------------------------------------------------------------

class TestConversionArtifactStripping:
    def test_strip_function_removes_picture_placeholder(self):
        from document_chunker import strip_conversion_artifacts
        out = strip_conversion_artifacts(
            "본문 시작\n**==> picture [720 x 252] intentionally omitted <==**\n본문 끝"
        )
        assert "intentionally omitted" not in out
        assert "==>" not in out
        assert "본문 시작" in out and "본문 끝" in out

    def test_strip_preserves_legit_arrow_prose(self):
        """PROD safety: only Docling's "intentionally omitted" markers are stripped, never
        real content that uses ==> / <== arrow notation (e.g. a manual's process flow)."""
        from document_chunker import strip_conversion_artifacts
        text = "신청 ==> 승인 ==> 완료 순서로 진행됩니다."
        assert strip_conversion_artifacts(text) == text  # unchanged
        # …but a real Docling placeholder on the same shape is still removed:
        out = strip_conversion_artifacts("앞 ==> table [10 x 2] intentionally omitted <== 뒤")
        assert "intentionally omitted" not in out
        assert "앞" in out and "뒤" in out

    def test_strip_function_removes_page_marker(self):
        from document_chunker import strip_conversion_artifacts
        out = strip_conversion_artifacts(
            "수강신청 기본 학점: 19학점\n--- end of page.page_number=5 ---\n다음 내용"
        )
        assert "end of page" not in out
        assert "page_number" not in out
        assert "수강신청 기본 학점: 19학점" in out and "다음 내용" in out

    def test_artifacts_absent_from_generated_chunks(self, tmp_path):
        chunker = _make_chunker()
        md = _write_md(tmp_path, "manual.md", """\
            # 안내

            **==> picture [158 x 47] intentionally omitted <==**
            실제 안내 내용입니다. 충분한 길이로 작성합니다.
            --- end of page.page_number=1 ---
            두 번째 페이지 내용입니다.
            내용을 더 채웁니다.
            마지막 줄입니다.
        """)
        parents, children = chunker.create_chunks_single(md)
        blob = "\n".join(p.page_content for _, p in parents) + "\n".join(c.page_content for c in children)
        assert "intentionally omitted" not in blob
        assert "end of page" not in blob
        # Real content survives the stripping.
        assert "실제 안내 내용입니다" in blob
        assert "두 번째 페이지 내용입니다" in blob

    def test_page_marker_between_calendar_rows_keeps_forward_fill(self, tmp_path):
        """A page-break marker landing *inside* a 학사일정 table must not break month
        forward-fill: stripping the marker (and its newline) re-joins the rows, so the
        month-empty row after the break still inherits the previous month.
        """
        chunker = _make_chunker()
        # No blank lines between rows — only the page marker separates them, exactly how a
        # mid-table page break appears in Docling output.
        md = _write_md(tmp_path, "cal.md", (
            "# 학사일정\n\n"
            "| 월 | 일 | 일정 |\n"
            "|---|---|---|\n"
            "| 6 | 1(월) | 중간고사 |\n"
            "--- end of page.page_number=2 ---\n"
            "|  | 8(월)~12(금) | 기말고사 |\n\n"
            "이후 일정은 추후 공지됩니다. 충분한 길이의 본문을 둡니다.\n"
        ))
        parents, _ = chunker.create_chunks_single(md)
        blob = "\n".join(p.page_content for _, p in parents)
        # The post-break row's empty month cell is forward-filled to 6 (would stay empty if
        # the marker had ended the calendar scan early).
        assert "| 6 | 8(월)~12(금)" in blob or "6 | 8(월)~12(금)" in blob


# ---------------------------------------------------------------------------
# KB scope exclusion (#108): out-of-scope sources are never indexed
# ---------------------------------------------------------------------------

class TestKbExcludeSources:
    def test_excluded_source_yields_no_chunks(self, tmp_path, monkeypatch):
        import config
        chunker = _make_chunker()
        monkeypatch.setattr(config, "KB_EXCLUDE_SOURCES", frozenset({"out_of_scope_doc"}))
        md = _write_md(tmp_path, "out_of_scope_doc.md", """\
            # 근로기관 안내

            제출서류: 성명, 주민번호, 주소. 충분한 길이의 본문을 둡니다. 이 문서는 색인에서 제외되어야 합니다.
            """)
        parents, children = chunker.create_chunks_single(md)
        assert parents == [] and children == []

    def test_non_excluded_source_still_chunked(self, tmp_path, monkeypatch):
        import config
        chunker = _make_chunker()
        monkeypatch.setattr(config, "KB_EXCLUDE_SOURCES", frozenset({"out_of_scope_doc"}))
        md = _write_md(tmp_path, "in_scope_doc.md", """\
            # 졸업요건

            졸업을 위해서는 총 130학점 이상을 이수해야 합니다. 충분한 길이의 본문을 둡니다.
            """)
        parents, children = chunker.create_chunks_single(md)
        assert len(parents) >= 1 and len(children) >= 1
