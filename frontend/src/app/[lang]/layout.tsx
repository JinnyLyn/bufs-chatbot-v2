import { AuthProvider } from "@/hooks/useAuth";
import type { Lang } from "@/lib/types";

export default async function LangLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ lang: string }>;
}) {
  const { lang: rawLang } = await params;
  const lang = (rawLang === "en" ? "en" : "ko") as Lang;

  // 로그인 상태는 이 세그먼트 전체가 공유한다 — 채팅·로그인·회원가입 화면이
  // 각자 다른 상태를 들고 있지 않도록.
  return <AuthProvider lang={lang}>{children}</AuthProvider>;
}
