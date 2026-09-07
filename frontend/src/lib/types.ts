// 답변 요청이 실패한 이유. 문구는 i18n `chat.err.*` 에 있고(보고서 §16 확정 문구),
// `busy` 만 다시 시도 버튼을 Retry-After 동안 잠근다.
export type ChatErrorKind = "busy" | "timeout" | "disconnect" | "server";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  /** 실패 안내 말풍선. content 는 표시 문구, 다시 시도는 마지막 메시지일 때만 붙는다. */
  errorKind?: ChatErrorKind;
  /** 연결이 끊기기 전까지 받은 부분 답변. 다시 시도가 성공하면 걷어낸다. */
  partial?: boolean;
  rated?: boolean;
  rating?: number;
  sourceUrls?: SourceURL[];
  results?: SearchResultItem[];
  intent?: string;
  durationMs?: number;
}

export interface SourceURL {
  title: string;
  url: string;
}

export interface SearchResultItem {
  text: string;
  score: number;
  source: string;
  page_number: number;
  doc_type: string;
  in_context: boolean;
  section_path?: string;
  source_url?: string;
  title?: string;
  post_date?: string;
  faq_id?: string;
  faq_question?: string;
  faq_answer?: string;
}

export interface SessionInfo {
  session_id: string;
  lang: string;
  user_profile: UserProfile | null;
  has_transcript: boolean;
  messages_count: number;
}

export interface UserProfile {
  student_id: string;
  department: string;
  student_type: string;
}

export interface StreamDoneData {
  answer: string;
  source_urls: SourceURL[];
  results: SearchResultItem[];
  intent: string;
  duration_ms: number;
}

// SSE `status` 이벤트 — 답변 생성 전 진행 상황(정보 전달용). 서버는 단계 키만 보내고
// 문구와 번역은 프론트가 갖는다. 알 수 없는 단계 키는 무시하면 되고, 이벤트가 아예 오지
// 않아도 기존 동작(제네릭 로딩 애니메이션) 그대로다.
export const STREAM_STAGES = ["searching", "reading", "writing", "checking"] as const;
export type StreamStage = (typeof STREAM_STAGES)[number];

export interface StreamProgress {
  stage: StreamStage;
  /** 실행된 검색 횟수(문서 건수가 아님) — 답변 뒤 출처 목록 개수와는 다른 값이다. */
  searches: number;
}

export type Lang = "ko" | "en";

// 학사 리포트 분석 (GET /api/transcript/analysis)
export interface AnalysisCategory {
  name: string;
  acquired: number;
  required: number;
  shortage: number;
  progress_pct: number;
  is_required: boolean;
}

export interface SemesterSummary {
  term: string;
  credits: number;
  course_count: number;
  gpa: number | null;
}

export interface RetakeCandidate {
  course: string;
  term: string;
  credits: number;
  grade: string;
}

export interface GraduationProjection {
  expected_term: string;
  semesters_remaining: number;
  can_early_graduate: boolean;
  early_eligible_reasons: string[];
  early_blocked_reasons: string[];
}

export interface ActionItem {
  type: string;
  severity: "info" | "warn" | "error";
  title: string;
  description: string;
  action_label: string | null;
  source: string;
  target_count: number | null;
  meta: Record<string, unknown>;
}

export interface TranscriptAnalysisData {
  has_transcript: boolean;
  profile: Record<string, unknown>;
  summary: { gpa: number; acquired: number; required: number; shortage: number; progress_pct: number };
  categories: AnalysisCategory[];
  semesters: SemesterSummary[];
  grade_distribution: Record<string, number>;
  retake_candidates: RetakeCandidate[];
  registration_limit: Record<string, unknown>;
  dual_major: Record<string, unknown>;
  graduation: GraduationProjection;
  actions: ActionItem[];
}

// 로그인 사용자 질문 이력 (GET /api/user/chat-history)
export interface ChatHistoryItem {
  id: number;
  session_id: string;
  question: string;
  answer: string;
  intent: string;
  rating: number | null;
  created_at: string;
}

export interface ChatHistoryResponse {
  total: number;
  items: ChatHistoryItem[];
}

// ── 신규 타입 (UI 리디자인) ──
export type TabId = "chat" | "report" | "notifications" | "profile";

export interface TranscriptStatus {
  has_transcript: boolean;
  remaining_seconds: number;
  masked_name: string;
  gpa: number;
  total_acquired: number;
  total_required: number;
  total_shortage: number;
  progress_pct: number;
  dual_major: string;
  dual_shortage: number;
}
