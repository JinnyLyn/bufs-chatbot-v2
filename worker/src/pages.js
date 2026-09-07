// Static HTML for the outage / maintenance page. No external assets: this page must
// render when nothing else does, and it is served under a CSP that allows only inline
// styles. Wording follows reports/CamChat-장애대응.pdf §11.2 / §16 ("서버 전체 장애").
//
// Contacts and the calendar link are duplicated from frontend/src/lib/constants.ts
// (CONTACT_GROUPS / EMERGENCY_CONTACTS / PORTAL_LINKS, added in PR #289) on purpose —
// the Worker has no build step and must not import the app. Change both places together.

const CALENDAR_URL = "https://m.bufs.ac.kr/popup/Haksa_Iljeong.aspx?gbn=";
const HOME_URL = "https://www.bufs.ac.kr/";
const AREA = "051-509";

// Either `exts` (department-level numbers) or `items` (named counters inside the department).
const CONTACTS = [
  { dept: "학사지원팀", exts: ["5182", "5183"] },
  { dept: "입학관리팀", exts: ["5305", "5306"] },
  { dept: "학생복지팀", items: [["보건진료소", "5444"]] },
  { dept: "캠퍼스관리팀", items: [["주차", "5412"], ["D동 멀강실", "5431"], ["I동 멀강실", "5434"]] },
];
// The emergency line in the notice is the academic office; derived, not retyped.
const ACADEMIC = CONTACTS[0];
const ACADEMIC_TEL = `${AREA}-${ACADEMIC.exts[0]}`;
const ACADEMIC_DISPLAY = `${AREA}-${ACADEMIC.exts.join("~")}`;

const CSS = `
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:#f1f5f9;color:#1e293b;font-family:"Noto Sans KR",-apple-system,BlinkMacSystemFont,
  "Apple SD Gothic Neo","Malgun Gothic",sans-serif;padding:24px 16px}
main{width:100%;max-width:520px;background:#fff;border:1px solid #e2e8f0;border-radius:20px;
  padding:28px 24px;box-shadow:0 10px 30px rgba(15,23,42,.06)}
.badge{display:inline-block;background:#fef3c7;color:#92400e;border:1px solid #fcd34d;
  border-radius:999px;font-size:12px;font-weight:700;padding:4px 12px;margin-bottom:14px}
h1{font-size:22px;margin:0 0 10px;letter-spacing:-.01em}
p{margin:0 0 10px;line-height:1.6;font-size:15px}
.muted{color:#475569;font-size:14px}
a{color:#1d4ed8}
.tel{font-weight:700;white-space:nowrap}
.actions{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0 6px}
.btn{display:inline-block;padding:10px 16px;border-radius:12px;font-weight:700;font-size:14px;
  text-decoration:none;border:1px solid #cbd5e1;color:#1e293b;background:#f8fafc}
.btn.primary{background:#2563eb;border-color:#2563eb;color:#fff}
.contacts{margin-top:22px;border-top:1px solid #e2e8f0;padding-top:14px}
.contacts h2{font-size:13px;color:#64748b;margin:0 0 8px;font-weight:700}
.contacts .hint{font-size:12px;color:#64748b;margin:0 0 8px}
.contacts ul{list-style:none;margin:0;padding:0}
.contacts li{display:flex;justify-content:space-between;align-items:baseline;gap:8px;
  padding:6px 0;border-bottom:1px dashed #e2e8f0;font-size:14px}
.contacts li:last-child{border-bottom:0}
.contacts .sub{color:#475569;font-size:13px;padding-left:10px}
.ext a{display:inline-block;background:#eff6ff;color:#1d4ed8;border-radius:8px;padding:2px 8px;
  font-weight:700;text-decoration:none;margin-left:4px;font-variant-numeric:tabular-nums}
footer{margin-top:16px;font-size:11px;color:#94a3b8}
`;

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function contactRows() {
  const ext = (n) => `<a href="tel:${AREA}-${n}" aria-label="${AREA}-${n}">${n}</a>`;
  return CONTACTS.map((c) => {
    if (c.exts) {
      return `<li><span>${esc(c.dept)}</span><span class="ext">${c.exts.map(ext).join("")}</span></li>`;
    }
    return (
      `<li><span>${esc(c.dept)}</span></li>` +
      c.items.map(([label, n]) => `<li><span class="sub">${esc(label)}</span><span class="ext">${ext(n)}</span></li>`).join("")
    );
  }).join("");
}

function shell({ title, badge, headline, lines, retryAfterS }) {
  return `<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta http-equiv="refresh" content="${retryAfterS}">
<title>${esc(title)}</title>
<style>${CSS}</style>
</head>
<body>
<main>
  <div class="badge">${esc(badge)}</div>
  <h1>${esc(headline)}</h1>
  ${lines.map((l) => `<p>${l}</p>`).join("\n  ")}
  <p class="muted">급한 학사 문의는 학사지원팀 <a class="tel" href="tel:${ACADEMIC_TEL}">${ACADEMIC_DISPLAY}</a>으로 연락해 주세요.</p>
  <div class="actions">
    <a class="btn primary" href="/">다시 접속</a>
    <a class="btn" href="${CALENDAR_URL}" target="_blank" rel="noopener noreferrer">학사일정표</a>
    <a class="btn" href="${HOME_URL}" target="_blank" rel="noopener noreferrer">부산외국어대학교 홈페이지</a>
  </div>
  <section class="contacts">
    <h2>주요 연락처</h2>
    <p class="hint">${AREA}-내선 · 번호를 누르면 바로 연결돼요</p>
    <ul>${contactRows()}</ul>
  </section>
  <footer>BUFS CamChat · 이 페이지는 ${Math.round(retryAfterS / 60)}분 뒤 자동으로 다시 접속을 시도합니다.</footer>
</main>
</body>
</html>
`;
}

/** 서버(터널·프론트엔드) 자체에 닿을 수 없을 때. */
export function outagePage(retryAfterS) {
  return shell({
    title: "챗봇 점검 중 · BUFS CamChat",
    badge: "일시적인 접속 장애",
    headline: "챗봇 점검 중입니다.",
    lines: ["현재 일시적으로 서비스에 접속할 수 없습니다. 잠시 후 다시 접속해 주세요."],
    retryAfterS,
  });
}

/** 운영자가 점검 모드를 켰을 때(계획 점검, 장시간 장애). */
export function maintenancePage(retryAfterS) {
  return shell({
    title: "챗봇 점검 중 · BUFS CamChat",
    badge: "점검 진행 중",
    headline: "챗봇 점검 중입니다.",
    lines: ["예정된 점검으로 잠시 이용할 수 없습니다. 점검이 끝나면 다시 접속해 주세요."],
    retryAfterS,
  });
}
