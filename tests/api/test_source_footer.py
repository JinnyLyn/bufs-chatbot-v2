"""Unit tests for api/sources.strip_source_footer — the structural backstop
behind the prompt-level 출처 섹션 금지 (the SourcePanel shows sources from metadata,
so a footer leaked by a disobedient generation must never reach the done payload).

Lives in api/sources (imports only `re`) so this stays collectible in the offline
CI job — importing api.agent_stream would pull the whole RAGSystem chain
(langchain_ollama etc.), which offline CI does not install.
"""
import time

from api.sources import strip_source_footer as _strip_source_footer

BODY = "휴학은 등록 기간 내에 학생포털에서 신청할 수 있습니다."


class TestStripsTrailingFooter:
    def test_canonical_footer_hr_bold_bullets(self):
        answer = f"{BODY}\n\n---\n**출처:**\n- 2026학년도2학기학사안내.pdf\n- 수강신청 FAQ.pdf"
        assert _strip_source_footer(answer) == BODY

    def test_footer_without_horizontal_rule(self):
        answer = f"{BODY}\n\n**출처:**\n- 2026학년도2학기학사안내.pdf"
        assert _strip_source_footer(answer) == BODY

    def test_plain_unbolded_heading(self):
        answer = f"{BODY}\n\n출처:\n- 학사안내.pdf"
        assert _strip_source_footer(answer) == BODY

    def test_english_sources_heading(self):
        answer = f"{BODY}\n\n---\nSources:\n- guide.pdf"
        assert _strip_source_footer(answer) == BODY

    def test_bare_filename_lines_without_bullets(self):
        answer = f"{BODY}\n\n**출처:**\n2026학년도2학기학사안내.pdf\n수강신청 FAQ.pdf"
        assert _strip_source_footer(answer) == BODY

    def test_numbered_list_items(self):
        answer = f"{BODY}\n\n**출처:**\n1. 학사안내.pdf\n2. FAQ.docx"
        assert _strip_source_footer(answer) == BODY

    def test_trailing_whitespace_after_footer(self):
        answer = f"{BODY}\n\n---\n**출처:**\n- 학사안내.pdf\n\n  "
        assert _strip_source_footer(answer) == BODY


class TestLeavesAnswerAlone:
    def test_answer_without_footer_unchanged(self):
        assert _strip_source_footer(BODY) == BODY

    def test_mid_text_source_mention_kept(self):
        answer = f"출처 확인이 필요하면 학사지원팀에 문의하세요.\n\n{BODY}"
        assert _strip_source_footer(answer) == answer

    def test_source_heading_followed_by_prose_kept(self):
        # A heading NOT followed by list/filename lines is answer content, not a footer.
        answer = f"{BODY}\n\n출처:\n이 내용은 학사팀 공지에 근거합니다."
        assert _strip_source_footer(answer) == answer

    def test_footer_only_answer_is_never_blanked(self):
        answer = "---\n**출처:**\n- 학사안내.pdf"
        assert _strip_source_footer(answer) == answer

    def test_empty_answer(self):
        assert _strip_source_footer("") == ""


class TestLinearScan:
    def test_adversarial_repetition_completes_fast(self):
        # CodeQL py/redos flagged the earlier single-regex version: exponential
        # backtracking on many repetitions of lines matching both the list-item and
        # the filename alternation. The line scan must stay linear on that input.
        answer = BODY + "\n\n**출처:**\n" + ("*\t.md\n" * 10_000)
        t0 = time.monotonic()
        result = _strip_source_footer(answer)
        assert time.monotonic() - t0 < 1.0
        assert result == BODY

    def test_many_plain_lines_unchanged(self):
        answer = "\n".join(f"본문 문장 {i}입니다." for i in range(10_000))
        t0 = time.monotonic()
        assert _strip_source_footer(answer) == answer
        assert time.monotonic() - t0 < 1.0
