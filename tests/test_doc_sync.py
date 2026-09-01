"""Tests for scripts/doc_sync.sh — pdfs/ 제어면 기반 KB 문서 추가/은퇴/복원.

실제 변환·색인은 무거워서(임베딩 모델) DOC_SYNC_REINDEX_CMD / DOC_SYNC_INGEST_CMD 로
스텁하고, 파일 이동·매칭·가드 로직만 검증한다. DOC_SYNC_ROOT 로 tmp 레포를 가리킨다.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts" / "doc_sync.sh")


@pytest.fixture()
def kb(tmp_path):
    """실레포 축소판: md 3종(원본 매칭 1, md 전용 2) + 원본 pdf 1."""
    (tmp_path / "markdown_docs" / "archive").mkdir(parents=True)
    (tmp_path / "pdfs" / "archive").mkdir(parents=True)
    (tmp_path / "markdown_docs" / "2026학년도1학기학사안내.md").write_text("일학기", encoding="utf-8")
    (tmp_path / "markdown_docs" / "수강신청 FAQ.md").write_text("faq", encoding="utf-8")
    (tmp_path / "markdown_docs" / "glossary.md").write_text("용어", encoding="utf-8")
    # 원본 파일명은 언더스코어 인코딩 (실레포와 동일한 편차)
    (tmp_path / "pdfs" / "2026학년도_1학기_학사안내.pdf").write_bytes(b"%PDF-fake")
    return tmp_path


def run(root, *args, reindex_log=None, ingest_log=None, check=True):
    env = os.environ.copy()
    env["DOC_SYNC_ROOT"] = str(root)
    env["DOC_SYNC_SKIP_PORT_CHECK"] = "1"
    env["DOC_SYNC_REINDEX_CMD"] = f"echo reindex >> {reindex_log or root / 'reindex.log'}"
    env["DOC_SYNC_INGEST_CMD"] = f"echo ingest >> {ingest_log or root / 'ingest.log'}"
    p = subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise AssertionError(f"doc_sync {args} failed rc={p.returncode}\n{p.stdout}\n{p.stderr}")
    return p


class TestStatus:
    def test_clean_state_no_actions(self, kb):
        p = run(kb, "status")
        assert "활성 문서(markdown_docs/*.md): 3" in p.stdout
        assert "[은퇴 예정 — pdfs/archive/ 기준] 0건" in p.stdout
        assert "[md 없음 — 'doc_sync.sh add <파일>' 로 추가 필요] 0건" in p.stdout

    def test_underscore_original_pairs_with_plain_md(self, kb):
        """언더스코어 원본 ↔ 무공백 md 정규화 매칭 — '신규 문서'로 오판하면 안 됨."""
        p = run(kb, "status")
        assert "md 없음 — 'doc_sync.sh add" in p.stdout
        assert "2026학년도_1학기_학사안내.pdf" not in p.stdout

    def test_unmatched_original_reported_as_needing_add(self, kb):
        (kb / "pdfs" / "새로운_안내문.pdf").write_bytes(b"%PDF-fake")
        p = run(kb, "status")
        assert "새로운_안내문.pdf" in p.stdout

    def test_stale_marker_warned(self, kb):
        (kb / "pdfs" / "archive" / "오타난_이름.pdf").write_bytes(b"")
        p = run(kb, "status")
        assert "오타난_이름.pdf" in p.stdout and "경고" in p.stdout

    def test_rejects_unknown_command_and_args(self, kb):
        assert run(kb, "nuke", check=False).returncode != 0
        assert run(kb, "apply", "--force", check=False).returncode != 0
        assert run(kb, "status", "extra", check=False).returncode != 0


class TestApply:
    def test_retire_via_archive_marker(self, kb):
        # md 전용 문서를 마커 파일로 은퇴 (원본 pdf 없어도 동작해야 함)
        (kb / "pdfs" / "archive" / "수강신청 FAQ.pdf").write_bytes(b"")
        p = run(kb, "apply")
        assert not (kb / "markdown_docs" / "수강신청 FAQ.md").exists()
        assert (kb / "markdown_docs" / "archive" / "수강신청 FAQ.md").exists()
        assert (kb / "reindex.log").exists(), "변경이 있으면 reindex 해야 함"
        assert "커밋" in p.stdout

    def test_retire_via_underscore_original(self, kb):
        os.rename(kb / "pdfs" / "2026학년도_1학기_학사안내.pdf",
                  kb / "pdfs" / "archive" / "2026학년도_1학기_학사안내.pdf")
        run(kb, "apply")
        assert (kb / "markdown_docs" / "archive" / "2026학년도1학기학사안내.md").exists()

    def test_restore_when_original_moved_back(self, kb):
        (kb / "pdfs" / "archive" / "수강신청 FAQ.pdf").write_bytes(b"")
        run(kb, "apply")
        # 마커를 pdfs/ 로 되돌리면 md 도 복원
        os.rename(kb / "pdfs" / "archive" / "수강신청 FAQ.pdf", kb / "pdfs" / "수강신청 FAQ.pdf")
        run(kb, "apply")
        assert (kb / "markdown_docs" / "수강신청 FAQ.md").exists()
        assert not (kb / "markdown_docs" / "archive" / "수강신청 FAQ.md").exists()

    def test_no_change_no_reindex(self, kb):
        p = run(kb, "apply")
        assert "변경 없음" in p.stdout
        assert not (kb / "reindex.log").exists()

    def test_md_only_docs_untouched(self, kb):
        (kb / "pdfs" / "archive" / "수강신청 FAQ.pdf").write_bytes(b"")
        run(kb, "apply")
        assert (kb / "markdown_docs" / "glossary.md").exists(), "원본 없는 md 전용 문서는 건드리면 안 됨"
        assert (kb / "markdown_docs" / "2026학년도1학기학사안내.md").exists()

    def test_both_sides_conflict_aborts(self, kb):
        (kb / "pdfs" / "archive" / "2026학년도1학기학사안내.pdf").write_bytes(b"")
        p = run(kb, "apply", check=False)
        assert p.returncode != 0 and "양쪽" in p.stderr
        # 아무 이동도 일어나지 않아야 함
        assert (kb / "markdown_docs" / "2026학년도1학기학사안내.md").exists()

    def test_backend_up_without_restart_aborts_before_moving(self, kb):
        (kb / "pdfs" / "archive" / "수강신청 FAQ.pdf").write_bytes(b"")
        env = os.environ.copy()
        env["DOC_SYNC_ROOT"] = str(kb)
        env["DOC_SYNC_REINDEX_CMD"] = "true"
        # 포트체크 스킵 없이 + lsof 를 '항상 떠있음' 스텁으로 대체
        fake_bin = kb / "bin"; fake_bin.mkdir()
        lsof = fake_bin / "lsof"
        lsof.write_text("#!/bin/sh\necho 12345\nexit 0\n")
        lsof.chmod(lsof.stat().st_mode | stat.S_IEXEC)
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        p = subprocess.run(["bash", SCRIPT, "apply"], capture_output=True, text=True, env=env)
        assert p.returncode != 0 and "Qdrant 락" in p.stderr
        assert (kb / "markdown_docs" / "수강신청 FAQ.md").exists(), "중단 시 파일 이동 없어야 함"


class TestAdd:
    def test_add_new_doc_calls_ingest_and_copies_original(self, kb, tmp_path):
        src = tmp_path / "외부" / "신규_공지.pdf"
        src.parent.mkdir()
        src.write_bytes(b"%PDF-fake")
        run(kb, "add", str(src))
        assert (kb / "ingest.log").exists()
        assert (kb / "pdfs" / "신규_공지.pdf").exists(), "원본을 pdfs/ 에 보관해야 함"

    def test_add_existing_doc_skipped(self, kb):
        p = run(kb, "add", str(kb / "pdfs" / "2026학년도_1학기_학사안내.pdf"))
        assert "skip" in p.stdout
        assert not (kb / "ingest.log").exists(), "매칭되는 md 가 있으면 ingest 호출 금지"

    def test_add_retired_doc_aborts_with_restore_guidance(self, kb):
        (kb / "pdfs" / "archive" / "수강신청 FAQ.pdf").write_bytes(b"")
        run(kb, "apply")
        p = run(kb, "add", str(kb / "pdfs" / "archive" / "수강신청 FAQ.pdf"), check=False)
        assert p.returncode != 0 and "복원" in p.stderr

    def test_add_unsupported_extension_aborts(self, kb):
        bad = kb / "문서.hwp"
        bad.write_bytes(b"hwp")
        assert run(kb, "add", str(bad), check=False).returncode != 0

    def test_add_short_name_not_swallowed_by_longer_md(self, kb):
        """리뷰 CONFIRMED 회귀: '수강신청.pdf'(신규)가 '수강신청 FAQ.md'에 prefix
        오매칭돼 조용히 skip 되면 안 됨 — 신규로 취급해 ingest 호출해야 함."""
        src = kb / "수강신청.pdf"
        src.write_bytes(b"%PDF-fake")
        p = run(kb, "add", str(src))
        assert "skip" not in p.stdout
        assert (kb / "ingest.log").exists()

    def test_version_suffix_original_still_pairs(self, kb):
        """'…_0723' 류 버전 접미가 붙은 원본은 기존 md 와 페어링 유지 (skip)."""
        src = kb / "2026학년도_1학기_학사안내_0301.pdf"
        src.write_bytes(b"%PDF-fake")
        p = run(kb, "add", str(src))
        assert "skip" in p.stdout
        assert not (kb / "ingest.log").exists()

    def test_add_all_skipped_ingest_fails_loudly(self, kb, tmp_path):
        """ingest.py 는 문서별 실패를 Skipped 로 세고 rc=0 — Added=0 이면 실패로 승격."""
        src = tmp_path / "신규문서.pdf"
        src.write_bytes(b"%PDF-fake")
        env = os.environ.copy()
        env["DOC_SYNC_ROOT"] = str(kb)
        env["DOC_SYNC_SKIP_PORT_CHECK"] = "1"
        env["DOC_SYNC_INGEST_CMD"] = "echo 'Done. Added=0  Skipped=1' #"
        p = subprocess.run(["bash", SCRIPT, "add", str(src)], capture_output=True, text=True, env=env)
        assert p.returncode != 0 and "Added=0" in p.stderr

    def test_short_marker_warns_instead_of_wrong_retire(self, kb):
        """짧은 마커 '수강신청'이 FAQ md 를 은퇴시키면 안 됨 — 미매칭 경고로 남아야 함."""
        (kb / "pdfs" / "archive" / "수강신청.pdf").write_bytes(b"")
        p = run(kb, "apply")
        assert (kb / "markdown_docs" / "수강신청 FAQ.md").exists()
        assert "경고" in p.stdout
