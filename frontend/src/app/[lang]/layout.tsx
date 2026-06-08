export default function LangLayout({ children, params }: { children: React.ReactNode; params: Promise<{ lang: string }> }) {
  return <>{children}</>;
}
