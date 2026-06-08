export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
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
