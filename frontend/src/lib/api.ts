const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "";

/** HTTP 상태를 그대로 들고 다니는 에러 — 호출부가 메시지 문자열을 파싱하지 않아도 되도록
 *  (401이면 토큰 정리 등, 상태별 분기가 필요하다). */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json", ...options?.headers },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail || `API error ${res.status}`);
  }
  return res.json();
}

export function sseUrl(path: string, params: Record<string, string>): string {
  const qs = new URLSearchParams(params).toString();
  return `${BASE_URL}${path}?${qs}`;
}

export { BASE_URL };
