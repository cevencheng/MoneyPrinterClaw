"use client";

/**
 * 对话主页 / → 渲染 UnifiedSidebar + Thread + TokenUsageDisplay
 *
 * 跨路由保活由 root layout 的 ClientShell 保证（AssistantRuntimeProvider 不随路由卸载）。
 */

import { UnifiedSidebar } from "@/components/layout/UnifiedSidebar";
import { useAppContext, TokenUsageDisplay } from "@/components/layout/ClientShell";
import { Thread } from "@/components/thread";

export default function Home() {
  const { tokenUsage } = useAppContext();
  return (
    <div className="flex h-full w-full">
      <UnifiedSidebar />
      <main className="flex min-w-0 flex-1 flex-col">
        <div className="min-h-0 flex-1">
          <Thread />
        </div>
        <TokenUsageDisplay usage={tokenUsage} />
      </main>
    </div>
  );
}