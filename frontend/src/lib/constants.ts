export const PORTAL_LINKS = [
  { icon: "\uD83D\uDDA5\uFE0F", key: "portal.register", url: "https://sugang.bufs.ac.kr/Login.aspx", iconName: "Monitor" },
  { icon: "\uD83D\uDCCA", key: "portal.student", url: "https://m.bufs.ac.kr/default.aspx?ReturnUrl=%2f", iconName: "BarChart3" },
  { icon: "\uD83D\uDCC5", key: "portal.calendar", url: "https://m.bufs.ac.kr/popup/Haksa_Iljeong.aspx?gbn=", iconName: "Calendar" },
  { icon: "\uD83D\uDCE2", key: "portal.notices", url: "https://www.bufs.ac.kr/bbs/board.php?bo_table=notice_aca", iconName: "Megaphone" },
];

export const QUICK_FEATURES_BASE = [
  { labelKey: "qf.register", questionKey: "qf.register_q", iconName: "CalendarPlus", bgColor: "bg-orange-50", iconColor: "text-orange-500" },
  { labelKey: "qf.grades", questionKey: "qf.grades_q", iconName: "ClipboardList", bgColor: "bg-green-50", iconColor: "text-green-500" },
  { labelKey: "qf.schedule", questionKey: "qf.schedule_q", iconName: "BookOpen", bgColor: "bg-blue-50", iconColor: "text-blue-500" },
  { labelKey: "qf.faq", questionKey: "qf.faq_q", iconName: "GraduationCap", bgColor: "bg-purple-50", iconColor: "text-purple-500" },
];

export const QUICK_FEATURES_PERSONAL = [
  { labelKey: "qf.shortage", questionKey: "qf.shortage_q", icon: "\uD83C\uDFAF", iconName: "Target", bgColor: "bg-red-50", iconColor: "text-red-500" },
  { labelKey: "qf.retake", questionKey: "qf.retake_q", icon: "\uD83D\uDD01", iconName: "RotateCcw", bgColor: "bg-cyan-50", iconColor: "text-cyan-500" },
  { labelKey: "qf.semester", questionKey: "qf.semester_q", icon: "\uD83D\uDCDA", iconName: "BookOpen", bgColor: "bg-indigo-50", iconColor: "text-indigo-500" },
  { labelKey: "qf.graduation", questionKey: "qf.graduation_q", icon: "\uD83C\uDF93", iconName: "GraduationCap", bgColor: "bg-amber-50", iconColor: "text-amber-500" },
];

// ── 학과/전공 데이터 (onboarding + register 공유) ──
export type DeptOption = { ko: string; en: string };
export type CollegeGroup = { ko: string; en: string; depts: DeptOption[] };

export const COLLEGES: CollegeGroup[] = [
  { ko: "유럽미주대학", en: "College of European and American Studies", depts: [
    { ko: "영어학부", en: "Department of English" }, { ko: "영어전공", en: "English Major" }, { ko: "영어통번역전공", en: "English Interpretation and Translation Major" },
    { ko: "유럽학부", en: "Department of European Studies" }, { ko: "프랑스어전공", en: "French Language Major" }, { ko: "독일어전공", en: "German Language Major" },
    { ko: "스페인어전공", en: "Spanish Language Major" }, { ko: "포르투갈(브라질)어전공", en: "Portuguese Language Major" }, { ko: "이탈리아어전공", en: "Italian Language Major" },
    { ko: "러시아어전공", en: "Russian Language Major" }, { ko: "유럽지역통상전공", en: "European Regional Trade Major" },
  ]},
  { ko: "아시아대학", en: "College of Asian Studies", depts: [
    { ko: "일본어융합학부", en: "Department of Japanese Studies" }, { ko: "한일문화콘텐츠전공", en: "Korean-Japanese Cultural Content Major" },
    { ko: "비즈니스일본어전공", en: "Business Japanese Major" }, { ko: "일본어IT전공", en: "Japanese IT Major" },
    { ko: "중국학부", en: "Department of Chinese Studies" }, { ko: "중국어전공", en: "Chinese Language Major" }, { ko: "중국지역통상전공", en: "Chinese Regional Trade Major" },
    { ko: "태국어전공", en: "Thai Language Major" }, { ko: "인도네시아·말레이시아전공", en: "Indonesian and Malay Language Major" },
    { ko: "베트남어전공", en: "Vietnamese Language Major" }, { ko: "미얀마어전공", en: "Burmese Language Major" },
    { ko: "인도어전공", en: "Hindi Language Major" }, { ko: "아랍어전공", en: "Arabic Language Major" }, { ko: "튀르키예어전공", en: "Turkish Language Major" },
  ]},
  { ko: "사회과학대학", en: "College of Social Sciences", depts: [
    { ko: "사회복지전공", en: "Social Welfare Major" }, { ko: "상담심리전공", en: "Counseling Psychology Major" },
    { ko: "경찰행정전공", en: "Police Administration Major" }, { ko: "사이버경찰전공", en: "Cyber Police Major" },
    { ko: "한국어교육전공", en: "Korean Language Education Major" }, { ko: "외교전공", en: "Diplomacy Major" },
    { ko: "국제개발협력전공", en: "International Development Cooperation Major" }, { ko: "글로벌인재융합전공", en: "Global Talent Convergence Major" },
    { ko: "글로벌미래융합학부", en: "Global Future Convergence School" }, { ko: "스포츠재활전공", en: "Sports Rehabilitation Major" },
    { ko: "사회체육전공", en: "Community Sports Major" }, { ko: "시민영어교육학과", en: "English Education for Citizens" },
    { ko: "글로벌문화비즈니스전공", en: "Global Culture Business Major" },
  ]},
  { ko: "상경대학", en: "College of Business and Economics", depts: [
    { ko: "경영전공", en: "Business Administration Major" }, { ko: "회계전공", en: "Accounting Major" },
    { ko: "경제금융전공", en: "Economics and Finance Major" }, { ko: "국제사무전공", en: "International Affairs and Secretary Major" },
    { ko: "국제마케팅전공", en: "International Marketing Major" }, { ko: "국제무역전공", en: "International Trade Major" },
    { ko: "국제문화관광전공", en: "International Cultural Tourism Major" }, { ko: "호텔·컨벤션전공", en: "Hotel and Convention Major" },
    { ko: "항공서비스전공", en: "Airline Service Major" }, { ko: "글로벌창업융합전공", en: "Global Startup Convergence Major" },
  ]},
  { ko: "디지털미디어·IT대학", en: "College of Digital Media and IT", depts: [
    { ko: "컴퓨터공학전공", en: "Computer Engineering Major" }, { ko: "소프트웨어전공", en: "Software Engineering Major" },
    { ko: "스마트융합보안전공", en: "Smart Convergence Security Major" }, { ko: "전자·인공지능융합전공", en: "Electronics and AI Convergence Major" },
    { ko: "빅데이터전공", en: "Big Data Major" }, { ko: "글로벌웹툰콘텐츠전공", en: "Global Webtoon Content Major" },
    { ko: "영상콘텐츠융합전공", en: "Video Content Convergence Major" }, { ko: "스마트에너지·환경전공", en: "Smart Energy and Environment Major" },
  ]},
  { ko: "글로벌자유전공학부", en: "Global Liberal Major School", depts: [{ ko: "글로벌자유전공학부", en: "Global Liberal Major School" }] },
  { ko: "만오교양대학", en: "Liberal Arts College", depts: [{ ko: "만오교양대학 교학팀", en: "Liberal Arts College Office" }, { ko: "융합교양교육센터", en: "Convergence Liberal Arts Education Center" }] },
  { ko: "International College", en: "International College", depts: [{ ko: "International College", en: "International College" }] },
];

export const YEARS = Array.from({ length: 12 }, (_, i) => 2026 - i);

export const BOTTOM_TABS = [
  { id: "chat" as const, iconName: "MessageSquare", labelKey: "tab.chat" },
  { id: "report" as const, iconName: "GraduationCap", labelKey: "tab.report" },
  { id: "notifications" as const, iconName: "Bell", labelKey: "tab.notifications" },
  { id: "profile" as const, iconName: "User", labelKey: "tab.profile" },
];
