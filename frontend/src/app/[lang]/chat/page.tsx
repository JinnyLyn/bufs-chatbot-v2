"use client";
import { useRef, useEffect, useState, use } from "react";
import type { Lang } from "@/lib/types";
import { useSession } from "@/hooks/useSession";
import { useChat } from "@/hooks/useChat";
import ChatHeader from "@/components/chat/ChatHeader";
import ChatMessage from "@/components/chat/ChatMessage";
import StreamingMessage from "@/components/chat/StreamingMessage";
import ThinkingAnimation from "@/components/chat/ThinkingAnimation";
import ChatInput from "@/components/chat/ChatInput";
import WelcomeScreen from "@/components/chat/WelcomeScreen";
import SourcePanel from "@/components/chat/SourcePanel";
import Sidebar from "@/components/layout/Sidebar";

export default function ChatPage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang: rawLang } = use(params);
  const lang = (rawLang === "en" ? "en" : "ko") as Lang;

  const { sessionId, loading } = useSession(lang);
  const { messages, isStreaming, streamText, sendMessage, clearMessages } = useChat(sessionId);

  const [sidebarOpen, setSidebarOpen] = useState(
    typeof window !== "undefined" ? window.innerWidth >= 1024 : false
  );

  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streamText]);

  const handleToggleLang = () => {
    const newLang = lang === "ko" ? "en" : "ko";
    window.location.href = `/${newLang}/chat`;
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-white">
        <div className="flex gap-2">
          <div className="w-3 h-3 bg-blue-400 rounded-full animate-bounce" />
          <div className="w-3 h-3 bg-blue-400 rounded-full animate-bounce [animation-delay:0.2s]" />
          <div className="w-3 h-3 bg-blue-400 rounded-full animate-bounce [animation-delay:0.4s]" />
        </div>
      </div>
    );
  }

  const hasMessages = messages.length > 0 || isStreaming;

  return (
    <div className="flex h-dvh bg-white overflow-hidden">
      <Sidebar
        lang={lang}
        messages={messages}
        onSelectQuestion={sendMessage}
        onClearChat={clearMessages}
        onNewChat={clearMessages}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
      />

      {/* Main content area */}
      <div className="flex-1 flex flex-col min-w-0 h-full relative">
        <ChatHeader
          lang={lang}
          onToggleSidebar={() => setSidebarOpen(!sidebarOpen)}
          onToggleLang={handleToggleLang}
        />

        <main className="flex-1 overflow-y-auto pb-32">
          <div className="max-w-4xl mx-auto p-4 md:p-6">
            {!hasMessages ? (
              <WelcomeScreen lang={lang} onSelect={sendMessage} />
            ) : (
              <div className="space-y-6">
                {messages.map((msg, i) => (
                  <div key={i}>
                    <ChatMessage msg={msg} />
                    {msg.role === "assistant" && (
                      <div className="ml-13 mt-1">
                        <SourcePanel lang={lang} results={msg.results} sourceUrls={msg.sourceUrls} />
                      </div>
                    )}
                  </div>
                ))}
                {isStreaming &&
                  (streamText ? (
                    <StreamingMessage text={streamText} />
                  ) : (
                    <ThinkingAnimation lang={lang} />
                  ))}
                <div ref={bottomRef} />
              </div>
            )}
          </div>
        </main>

        <ChatInput lang={lang} onSend={sendMessage} disabled={isStreaming} />
      </div>
    </div>
  );
}
