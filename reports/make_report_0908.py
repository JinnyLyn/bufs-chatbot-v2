"""reports/REPORT_결과_0908.docx (+ .md) — 2026-09-02 성능 점검 보고서(제출본 REPORT_결과_0902.docx)를
그대로 두고, 그 뒤에 "장애 대응 계획과 구현 현황" 편을 붙인 통합 보고서.

앞부분은 제출본 docx 를 그대로 복사하므로(손으로 다듬은 문장 보존) 이 스크립트는 뒷부분만 안다.
행정 담당자가 읽는 문서: 기술 용어에는 뜻을 붙이고, 수치에는 근거(시험 일자·방법)를 단다.

    python reports/make_report_0908.py
"""
import copy
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

REPO = Path(__file__).resolve().parents[1]
REPORTS = REPO / "reports"
BASE = REPORTS / "REPORT_결과_0902.docx"
OUT_DOCX = REPORTS / "REPORT_결과_0908.docx"
OUT_MD = REPORTS / "REPORT_결과_0908.md"
DATE = "2026-09-08"

# (제목, 본문 문단들, 표 또는 None, 표 뒤 문단들)
SECTIONS = [
    (
        "한 줄 요약",
        [
            "9월 4일 점검에서 찾은 구멍 여섯 개 가운데 **급한 세 개(자동 재시작·알림, 외부 상태 주소, "
            "멈춘 화면)를 포함해 네 개를 막았고, 9월 7~8일에 실제 장애를 일으켜 동작을 확인했습니다.** "
            "서버 전체가 죽어도 학생은 영문 오류 대신 한국어 안내와 연락처를 보고, 프로세스가 죽으면 "
            "15초 안에 저절로 다시 뜨며, 2분마다 상태를 점검해 필요하면 재기동합니다.",
            "남은 두 개(학생 신고 채널, 자료 밖 질문에 붙는 안내 문구)와 담당자 확인이 필요한 항목"
            "(알림 채널 주소, 외부 모니터 등록, 연락처 최종 확인)은 아래 '남은 일'에 적었습니다.",
        ],
        None,
        [],
    ),
    (
        "장애가 나는 자리와 지금 학생이 보는 것 (구현 후)",
        [
            "학생 브라우저 → Cloudflare(maruvis.kr) → 터널(cloudflared) → 우리 서버(화면 :3000 / 백엔드 :8000 / "
            "AI 모델 ollama :11500, 공유 H100 GPU 한 조각). 9월 4일 표와 같은 자리를 구현 후 상태로 다시 적었습니다.",
        ],
        (
            ["어디가 죽나", "학생이 보는 것 (지금)", "자동 복구 (지금)"],
            [
                ["터널(cloudflared)", "한국어 안내 페이지 '챗봇 점검 중입니다' + 학사지원팀 전화 + 학사일정표 (Worker)", "있음 (5초 후 재시작)"],
                ["화면 서버(Next.js)", "같은 안내 페이지 (Worker)", "있음 (systemd, 15초 안에 재기동)"],
                ["백엔드(FastAPI)", "페이지는 뜨고, 질문하면 '응답을 불러오지 못했어요. 잠시 후 다시 시도해 주세요' + [다시 시도] + 연락처", "있음 (systemd 재기동 + 2분 점검)"],
                ["AI 모델(ollama·GPU)", "45초 뒤 '조금 더 걸리고 있어요' → 120초 뒤 다시 시도 안내", "있음 (2분 점검이 ollama 재기동). 공유 GPU를 남의 작업이 채운 경우는 관리자 요청"],
                ["서버 재부팅", "전체 다운 → 자동 기동", "있음 (systemd 사용자 유닛, Linger)"],
                ["동시 질문 8개 초과", "'지금 처리 중인 질문이 많습니다' + 10초 뒤 재시도 (버튼·입력창 잠금)", "해당 없음 (설계된 정상 동작)"],
            ],
        ),
        [],
    ),
    (
        "상황 1 — 우리 서버 문제: 구현한 것",
        [
            "**자동 재시작.** 스택을 '한 번 실행' 방식에서 프로세스별 systemd 유닛(ollama·백엔드·화면 서버)으로 바꿨습니다. "
            "프로세스가 죽으면 15초 뒤 저절로 다시 뜨고, 15분에 10번 넘게 죽으면 멈추고 알림을 보냅니다. "
            "9월 8일 운영 서버에 적용했습니다.",
            "**2분 점검(healthcheck).** 2분마다 백엔드·AI 모델·화면 서버를 실제로 호출해 봅니다. 2번 연속 실패하면 "
            "재기동하고(AI 모델만 문제면 ollama 도), 1시간에 3번 넘게 재기동하면 자동 재시작을 멈추고 사람을 부릅니다. "
            "재기동 직후 5분은 실패로 세지 않아, 켜지는 중인 서버를 다시 죽이지 않습니다. 계획 점검(자료 갱신·배포) 중에는 "
            "점검을 건너뛰어 일부러 내린 서버를 되살리지 않습니다.",
            "**알림.** 첫 실패·재기동·자동 재시작 중단·복구 때만 팀 채널(Discord 또는 Slack 웹훅)로 한 줄씩 보냅니다. "
            "유닛 자체가 기동에 실패해도 같은 채널로 갑니다. 웹훅 주소는 서버의 비공개 설정 파일에만 두며, "
            "**주소를 아직 받지 못해 지금은 서버 로그(logs/alerts.log)에만 남습니다.**",
            "**외부 상태 주소.** maruvis.kr/api/health 가 {\"status\":\"ok\"} 만 답합니다(모델명·경로 등 내부 정보 없음). "
            "무료 외부 모니터(UptimeRobot, 5분 간격)에 이 주소와 첫 화면 주소를 등록하면 서버 밖에서도 장애를 잡습니다 — "
            "등록 절차는 docs/observability/external-monitoring.md, **등록은 담당자 작업으로 남아 있습니다.**",
            "**학생 화면.** 45초 동안 진행이 없으면 '답변을 준비하는 데 시간이 조금 더 걸리고 있어요', 120초가 넘거나 연결이 "
            "끊기면 '응답을 불러오지 못했어요' + [다시 시도]. 받다 만 답변은 '여기까지 받은 답변입니다' 표시와 함께 남기고, "
            "다시 시도가 성공하면 걷어냅니다. 혼잡할 때는 버튼과 입력창을 10초 잠가 재시도 폭주를 막습니다. "
            "모든 오류 문구 아래에 학사지원팀 전화(누르면 연결)와 학사일정표 링크가 붙습니다. 9월 7일 운영 반영.",
        ],
        None,
        [],
    ),
    (
        "상황 2 — 우리 손 밖: 안내 페이지 (구현·운영 적용)",
        [
            "Cloudflare Worker 를 maruvis.kr 의 첫 화면·챗 화면 주소 앞에 두었습니다(9월 7일 운영 적용). 평소에는 요청을 그대로 "
            "넘기고, 우리 서버에 닿지 못하면 한국어 안내 페이지를 대신 보냅니다: '챗봇 점검 중입니다. 현재 일시적으로 서비스에 "
            "접속할 수 없습니다. 잠시 후 다시 접속해 주세요.' + 학사지원팀 전화 + 학사일정표·학교 홈페이지 버튼 + 부서별 "
            "내선(학사지원팀·입학관리팀·학생복지팀·캠퍼스관리팀). 5분마다 저절로 다시 접속을 시도합니다.",
            "외부 모니터가 장애를 알아채도록 안내 페이지는 '정상'(200)이 아니라 '점검 중'(503) 상태로 내려갑니다. "
            "API·답변 스트림·정적 파일은 Worker 를 거치지 않아 답변 속도에 영향이 없습니다.",
            "**점검 모드.** 계획 점검 때 명령 한 줄(npm run maintenance:on)로 안내 페이지를 켜고 끕니다. 재배포 없이 약 1분 안에 반영됩니다.",
            "한계는 그대로입니다. Cloudflare 자체 장애나 학교망에서 maruvis.kr 차단이면 안내 페이지도 못 띄우므로 학교 공지 채널을 씁니다.",
        ],
        None,
        [],
    ),
    (
        "상황 3 — 자료 밖 질문: 사이드바 연락처 (구현), 안내 문구 (후속)",
        [
            "사이드바의 대화 기록·초기화 자리를 **주요 연락처**(학사지원팀 5182·5183, 입학관리팀 5305·5306, 학생복지팀 보건진료소 5444, "
            "캠퍼스관리팀 주차 5412·D동 멀강실 5431·I동 멀강실 5434 — 국번 051-509, 누르면 바로 연결)와 **바로가기**"
            "(학사일정·학사공지·수강신청·학생포털·홈페이지)로 바꿨습니다. 첫 화면에는 '이런 정보를 안내해 드려요 / 안내하지 못해요' "
            "가이드를 두어 범위를 먼저 보여 줍니다. 언어 선택 화면과 예시 질문 버튼은 없앴습니다.",
            "거부 답변 뒤에 붙는 고정 안내 문구(범위·홈페이지·학사지원팀·학부과 전화)는 시행 계획 4번의 남은 절반으로, 다음 작업입니다.",
        ],
        None,
        [],
    ),
    (
        "모의 장애 시험 결과 (2026-09-07 ~ 08)",
        [
            "계획대로 배포 뒤 실제 장애를 일으켜 확인했습니다. 시험 4·5는 9월 7일 밤(Worker 운영 적용 직후), "
            "시험 1~3은 9월 8일 새벽(유닛 전환 직후)에 이용이 적은 시간을 골라 했습니다. 기록은 docs/OUTAGE_DRILLS.md 에 남기고, "
            "이후 월 1회 반복합니다.",
        ],
        (
            ["시험", "어떻게", "확인한 것", "소요"],
            [
                ["1 백엔드 강제 종료", "백엔드 프로세스를 강제로 죽임", "죽은 즉시 알림(로그). systemd 가 15초 뒤 다시 띄워 34초 뒤 정상 응답 (재기동 15초 + 기동 19초)", "34초"],
                ["2 백엔드 무응답", "프로세스는 살아 있지만 응답하지 못하게 멈춤", "2분 점검이 40초 뒤 첫 실패를 잡아 알림, 2번째 실패(2분 뒤)에서 백엔드·화면 서버 재기동. 새 프로세스로 정상 응답", "3분 11초 (점검 주기 2분 × 2회 + 재기동)"],
                ["3 화면 서버 종료", "화면 서버 프로세스를 강제로 죽임", "죽은 즉시 알림(로그). 12초 뒤 정상 (겹친 2분 점검이 systemd 보다 먼저 재기동)", "12초"],
                ["4 전체 서비스 중단", "터널(cloudflared)을 정지", "maruvis.kr 이 2초 뒤 한국어 안내 페이지(503 + Retry-After 300)를 보임. 연락처·학사일정표 표시. 터널 재시작 3초 뒤 정상 복귀", "접속 불가 총 7초"],
                ["5 점검 모드", "명령으로 점검 모드 켜고 끔", "켠 뒤 35초에 점검 안내, API 는 계속 정상. 끈 뒤 50초에 복귀", "반영 각 1분 이내"],
            ],
        ),
        [],
    ),
    (
        "시행 계획 진행 현황",
        [],
        (
            ["순서", "항목", "상태", "비고"],
            [
                ["1", "감시·자동 재시작·알림", "완료 (9/8 운영 적용)", "웹훅 주소만 담당자 확인 대기"],
                ["2", "외부 상태 주소", "완료 (/api/health)", "UptimeRobot 등록은 담당자 작업"],
                ["3", "화면 오류 처리", "완료 (9/7 운영 적용)", "45초 안내·120초 종료·다시 시도·연락처·혼잡 잠금"],
                ["4", "자료 밖 질문 안내 + 사이드바 연락처", "절반 완료", "사이드바 연락처 완료, 거부 답변 안내 문구는 후속"],
                ["5", "Worker 안내 페이지 + 점검 스위치", "완료 (9/7 운영 적용)", "실제 서버 정지 시험 통과"],
                ["6", "신고 채널 (👍/👎)", "미착수", "후속"],
                ["7", "소프트 타임아웃", "미착수", "정답률 비교 후 결정"],
                ["8", "대기 서버 확인", "해당 없음", "두 번째 GPU 서버 없음 — 자동 복구·감시 강화로 대체"],
            ],
        ),
        [],
    ),
    (
        "담당자 확인이 필요한 것 (남은 일)",
        [
            "1. **알림 채널** — 팀 Discord 또는 Slack 의 웹훅 주소 한 줄. 받는 즉시 서버에 넣습니다(코드 저장소에는 넣지 않음). 그 전까지 알림은 서버 로그에만 남습니다.",
            "2. **외부 모니터 등록** — UptimeRobot(무료)에 maruvis.kr 첫 화면과 maruvis.kr/api/health 를 5분 간격으로 등록하고 알림 받을 이메일·앱을 연결합니다(절차 문서 있음).",
            "3. **연락처 최종 확인** — 화면·안내 페이지에 실린 부서 내선(학사지원팀 5182·5183, 입학관리팀 5305·5306, 보건진료소 5444, 주차 5412, D동 멀강실 5431, I동 멀강실 5434)을 각 부서와 확인합니다.",
            "4. **Cloudflare 설정 한 번** — 대시보드에서 Worker 라우트의 'Fail open' 을 켜 두면 무료 요청 한도를 넘어도 사이트가 막히지 않습니다.",
        ],
        None,
        [],
    ),
    (
        "확정 문구 (구현됨)",
        [],
        (
            ["상황", "문구"],
            [
                ["45초 무진행", "답변을 준비하는 데 시간이 조금 더 걸리고 있어요. 잠시만 기다려 주세요."],
                ["120초 초과 · 연결 끊김", "응답을 불러오지 못했어요. 잠시 후 다시 시도해 주세요. [다시 시도]"],
                ["받다 만 답변", "여기까지 받은 답변입니다. 연결이 끊겨 뒷부분은 오지 않았어요."],
                ["혼잡", "지금 처리 중인 질문이 많습니다. 잠시 후 다시 시도해 주세요. (10초 뒤 재시도 가능)"],
                ["긴급 문의 (오류 문구 아래)", "급한 학사 문의는 학사지원팀 051-509-5182~5183으로 연락해 주세요. 학사일정은 학사일정표에서 확인할 수 있습니다."],
                ["서버 전체 다운 (안내 페이지)", "챗봇 점검 중입니다. 현재 일시적으로 서비스에 접속할 수 없습니다. 잠시 후 다시 접속해 주세요."],
                ["계획 점검 (안내 페이지)", "챗봇 점검 중입니다. 예정된 점검으로 잠시 이용할 수 없습니다. 점검이 끝나면 다시 접속해 주세요."],
            ],
        ),
        [],
    ),
]

FOOT = (
    f"구현 기준: 2026-09-07 ~ 08 운영 서버(maruvis.kr) 적용분 — 프론트엔드 장애 화면·사이드바(PR #289), "
    f"Cloudflare Worker 안내 페이지(#290), systemd 유닛·2분 점검·알림·/api/health(#293). "
    f"모의 장애 시험 기록: docs/OUTAGE_DRILLS.md. 계획 원본: 2026-09-04 장애 대응 계획 보고서."
)

HEAD_COLOR = RGBColor(0x0F, 0x5B, 0x4F)
SUB_COLOR = RGBColor(0x60, 0x6E, 0x6A)
FOOT_COLOR = RGBColor(0x6D, 0x7F, 0x78)


def _set_font(run, name="맑은 고딕", size=None, bold=False, color=None):
    run.font.name = name
    run.font.bold = bold
    if size:
        run.font.size = Pt(size)
    if color:
        run.font.color.rgb = color
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)


def _rich(par, text, size=10.5):
    for i, piece in enumerate(text.split("**")):
        if piece:
            _set_font(par.add_run(piece), size=size, bold=(i % 2 == 1))


def append_part(doc):
    """제출본 뒤에 2편을 붙인다 — 제목 크기는 1편 제목과 같게, 절 제목은 1편 절 제목과 같게."""
    doc.add_page_break()
    title = doc.add_paragraph()
    title.alignment = 1  # center
    _set_font(title.add_run("부산외대 학사 챗봇 — 장애 대응 계획과 구현 현황"), size=18, bold=True)
    sub = doc.add_paragraph()
    sub.alignment = 1
    _set_font(sub.add_run(f"{DATE} · 9월 4일 계획 기준, 운영 서버 적용·시험 결과"), size=9, color=SUB_COLOR)

    for heading, paras, table, tail in SECTIONS:
        h = doc.add_paragraph()
        h.paragraph_format.space_before = Pt(14)
        _set_font(h.add_run(heading), size=13, bold=True, color=HEAD_COLOR)
        for text in paras:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(6)
            _rich(p, text)
        if table:
            header, rows = table
            t = doc.add_table(rows=1, cols=len(header))
            t.style = "Light Grid Accent 1"
            for cell, label in zip(t.rows[0].cells, header):
                cell.text = ""
                _set_font(cell.paragraphs[0].add_run(label), size=10, bold=True)
            for row in rows:
                for cell, value in zip(t.add_row().cells, row):
                    cell.text = ""
                    _set_font(cell.paragraphs[0].add_run(value), size=10)
            doc.add_paragraph()
        for text in tail:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(6)
            _rich(p, text)

    f = doc.add_paragraph()
    f.paragraph_format.space_before = Pt(16)
    _set_font(f.add_run(FOOT), size=8.5, color=FOOT_COLOR)


def base_to_md(doc):
    """제출본(1편)을 md 로 옮긴다 — 문단은 그대로, 표는 마크다운 표로. 제목 구분은 글자 크기로 판단."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    out = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            text = p.text.strip()
            if not text:
                continue
            sizes = [r.font.size.pt for r in p.runs if r.font.size]
            size = max(sizes) if sizes else 10.5
            if size >= 16:
                out += [f"# {text}", ""]
            elif size >= 12:
                out += [f"## {text}", ""]
            elif size <= 9:
                out += [f"> {text}", ""]
            else:
                out += [text, ""]
        elif tag == "tbl":
            t = Table(child, doc)
            rows = [[c.text.replace("\n", " ") for c in r.cells] for r in t.rows]
            out += ["| " + " | ".join(rows[0]) + " |", "|" + "|".join(["---"] * len(rows[0])) + "|"]
            out += ["| " + " | ".join(r) + " |" for r in rows[1:]] + [""]
    return out


def part_to_md():
    out = ["", "---", "", "# 부산외대 학사 챗봇 — 장애 대응 계획과 구현 현황", "",
           f"> {DATE} · 9월 4일 계획 기준, 운영 서버 적용·시험 결과", ""]
    for heading, paras, table, tail in SECTIONS:
        out += [f"## {heading}", ""]
        for text in paras:
            out += [text, ""]
        if table:
            header, rows = table
            out += ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
            out += ["| " + " | ".join(r) + " |" for r in rows] + [""]
        for text in tail:
            out += [text, ""]
    out += ["---", "", FOOT, ""]
    return out


if __name__ == "__main__":
    doc = Document(BASE)
    md = base_to_md(doc)
    append_part(doc)
    doc.save(OUT_DOCX)
    OUT_MD.write_text("\n".join(md + part_to_md()), encoding="utf-8")
    print("생성 완료:", OUT_DOCX.name, OUT_DOCX.stat().st_size, "|", OUT_MD.name, OUT_MD.stat().st_size)
