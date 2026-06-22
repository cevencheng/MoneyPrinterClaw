"use client";

import { AssistantRuntimeProvider, useExternalStoreRuntime } from "@assistant-ui/react";
import {
  useExternalChatStore,
  onTokenUsage,
  type TokenUsage,
} from "@/lib/chat-adapter";
import { WebSearchToolUI } from "@/components/tools/web-search-tool";
import { VideoReviewToolUI } from "@/components/tools/video-review-tool";
import { VideoRenderToolUI } from "@/components/tools/video-render-tool";
import { createContext, useContext, useState, type ReactNode } from "react";

export type { TokenUsage };

// ── AppContext ─────────────────────────────────────────────────────────────

interface AppContextValue {
  tokenUsage: TokenUsage | null;
  pagination: ReturnType<typeof useExternalChatStore>["pagination"];
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (v: boolean) => void;
}

export const AppContext = createContext<AppContextValue>(null!);

export const useAppContext = () => {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useAppContext must be used inside <ClientShell>");
  return ctx;
};

// ── Token 用量显示 ────────────────────────────────────────────────────────

export const TokenUsageDisplay = ({ usage }: { usage: TokenUsage | null }) => {
  if (!usage) return null;
  const inT = usage.prompt_tokens ?? usage.input_tokens ?? 0;
  const outT = usage.completion_tokens ?? usage.output_tokens ?? 0;
  const total = usage.total_tokens ?? inT + outT;
  return (
    <div className="flex items-center justify-center gap-3 pb-3 text-xs text-muted-foreground/60">
      <span>
        输入 {inT} · 输出 {outT} · 共 {total} tokens
      </span>
    </div>
  );
};

// ── ClientShell ───────────────────────────────────────────────────────────

/**
 * 客户端壳——放在 root layout 内,持有跨路由保活的 chat state。
 *
 * 职责：
 * - 调一次 useExternalChatStore（pagination 唯一实例）
 * - 注入 AssistantRuntimeProvider + 3 个 ToolUI
 * - 通过 AppContext 下发 tokenUsage / pagination / sidebarCollapsed
 */
export const ClientShell = ({ children }: { children: ReactNode }) => {
  const [tokenUsage, setTokenUsage] = useState<TokenUsage | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  onTokenUsage(setTokenUsage);

  const { adapter, pagination } = useExternalChatStore();
  const runtime = useExternalStoreRuntime(adapter);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      {/* 工具的原生渲染器 */}
      <WebSearchToolUI />
      <VideoReviewToolUI />
      <VideoRenderToolUI />
      <AppContext.Provider
        value={{ tokenUsage, pagination, sidebarCollapsed, setSidebarCollapsed }}
      >
        {children}
      </AppContext.Provider>
    </AssistantRuntimeProvider>
  );
};