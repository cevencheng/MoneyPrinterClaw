"use client";

import { AssistantRuntimeProvider, useExternalStoreRuntime } from "@assistant-ui/react";
import {
  useExternalChatStore,
  onTokenUsage,
  type TokenUsage,
} from "@/lib/chat-adapter";
import { Thread } from "@/components/thread";
import { ThreadList } from "@/components/thread-list";
import { WebSearchToolUI } from "@/components/tools/web-search-tool";
import { VideoReviewToolUI } from "@/components/tools/video-review-tool";
import { VideoRenderToolUI } from "@/components/tools/video-render-tool";
import { PanelLeftCloseIcon, PanelLeftOpenIcon, SettingsIcon, UserIcon } from "lucide-react";
import Link from "next/link";
import Image from "next/image";
import { useState } from "react";

/** Token 用量显示 */
const TokenUsageDisplay = ({ usage }: { usage: TokenUsage | null }) => {
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

/** 侧栏顶部：产品 LOGO + 名称 + 折叠按钮 */
const SidebarHeader = ({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) => (
  <div className="shrink-0 border-b border-border/60 bg-muted/30 px-3 py-2.5 flex items-center gap-2">
    <Image
      src="/mpc-logo-cropped-v2.png"
      alt="MP Claw"
      width={36}
      height={36}
      className="size-9 shrink-0 rounded-md object-contain"
      priority
    />
    <span className="min-w-0 flex-1 truncate text-sm font-semibold text-foreground/90">
      MoneyPrinterClaw
    </span>
    <button
      type="button"
      onClick={onToggle}
      title={collapsed ? "展开侧栏" : "折叠侧栏"}
      className="size-7 shrink-0 rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground flex items-center justify-center"
    >
      <PanelLeftCloseIcon className="size-4" />
      <span className="sr-only">折叠侧栏</span>
    </button>
  </div>
);

/** 侧栏底部固定卡：左 avatar 占位 + 右设置按钮(跳 /settings) */
const SidebarFooter = ({ collapsed = false }: { collapsed?: boolean }) => (
  <div className="shrink-0 border-t border-border/60 bg-muted/40 px-3 py-2 flex items-center justify-between gap-2">
    {collapsed ? (
      <Link
        href="/settings"
        title="全局设置"
        className="size-8 shrink-0 rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground flex items-center justify-center mx-auto"
      >
        <SettingsIcon className="size-4" />
      </Link>
    ) : (
      <>
        <div className="flex items-center gap-2 min-w-0">
          <div className="size-8 shrink-0 rounded-full bg-muted-foreground/15 flex items-center justify-center text-muted-foreground">
            <UserIcon className="size-4" />
          </div>
          <div className="min-w-0 text-sm text-foreground/80 truncate">本地用户</div>
        </div>
        <Link
          href="/settings"
          title="全局设置"
          className="size-8 shrink-0 rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground flex items-center justify-center"
        >
          <SettingsIcon className="size-4" />
        </Link>
      </>
    )}
  </div>
);

/**
 * 主聊天界面
 *
 * 使用 useExternalStoreRuntime：后端 LangGraph Checkpointer 为唯一真实源，
 * 会话身份与消息读写全部由 useExternalChatStore 驱动（不再有模块全局桥）。
 */
export const Assistant = () => {
  const [tokenUsage, setTokenUsage] = useState<TokenUsage | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  onTokenUsage(setTokenUsage);

  const { adapter, pagination } = useExternalChatStore();
  const runtime = useExternalStoreRuntime(adapter);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      {/* 注册工具的原生渲染器：web_search 来源胶囊 + review_video_plan HITL 评审卡 + render_video 进度卡/成片 */}
      <WebSearchToolUI />
      <VideoReviewToolUI />
      <VideoRenderToolUI />
      <div className="flex h-full">
        {/* 左侧：会话列表 + 底部用户/设置卡；可折叠 */}
        <div
          className={
            sidebarCollapsed
              ? "w-14 shrink-0 border-r border-border/60 bg-muted/30 flex flex-col min-h-0 transition-[width] duration-200"
              : "w-64 shrink-0 border-r border-border/60 bg-muted/30 flex flex-col min-h-0 transition-[width] duration-200"
          }
        >
          {sidebarCollapsed ? (
            // 折叠态：顶部展开按钮 + 下方折叠版会话列表（仅新建按钮）
            <>
              <div className="shrink-0 border-b border-border/60 p-2.5 flex justify-center">
                <button
                  type="button"
                  onClick={() => setSidebarCollapsed(false)}
                  title="展开侧栏"
                  className="size-9 rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground flex items-center justify-center"
                >
                  <PanelLeftOpenIcon className="size-4" />
                  <span className="sr-only">展开侧栏</span>
                </button>
              </div>
              <div className="flex-1 min-h-0">
                <ThreadList pagination={pagination} collapsed />
              </div>
            </>
          ) : (
            <SidebarHeader
              collapsed={sidebarCollapsed}
              onToggle={() => setSidebarCollapsed(true)}
            />
          )}
          {/* 展开态才渲染完整会话列表（折叠态由上方 collapsed ThreadList 提供） */}
          {!sidebarCollapsed && (
            <div className="flex-1 min-h-0">
              <ThreadList pagination={pagination} />
            </div>
          )}
          <SidebarFooter collapsed={sidebarCollapsed} />
        </div>
        {/* 右侧：对话区域 */}
        <div className="flex-1 flex flex-col min-w-0">
          <div className="flex-1 min-h-0">
            <Thread />
          </div>
          <TokenUsageDisplay usage={tokenUsage} />
        </div>
      </div>
    </AssistantRuntimeProvider>
  );
};
