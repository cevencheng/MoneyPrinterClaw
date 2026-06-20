"use client";

import {
  useAssistantToolUI,
  type ToolCallMessagePartComponent,
} from "@assistant-ui/react";
import {
  ChevronDownIcon,
  ExternalLinkIcon,
  GlobeIcon,
  LoaderIcon,
} from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";

/** 单条搜索结果（与后端 web_search 工具返回结构一致） */
export interface WebSearchSource {
  title: string;
  url: string;
  snippet: string;
  siteName?: string;
  datePublished?: string;
}

/**
 * 后端 tool_result.result 是 JSON 字符串（LangGraph ToolMessage.content），
 * 这里统一解析成数组。
 */
function parseSources(result: unknown): WebSearchSource[] {
  if (!result) return [];
  let data: unknown = result;
  if (typeof data === "string") {
    try {
      data = JSON.parse(data);
    } catch {
      return [];
    }
  }
  return Array.isArray(data) ? (data as WebSearchSource[]) : [];
}

/** 从 URL 提取域名 */
function getDomain(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

const WebSearchRender: ToolCallMessagePartComponent = ({ args, result }) => {
  const query = (args as { query?: string } | null)?.query ?? "";
  const sources = parseSources(result);
  const count = sources.length;

  return (
    <Collapsible className="my-1 w-full">
      <CollapsibleTrigger
        className={cn(
          "group/trigger text-muted-foreground hover:text-foreground",
          "flex w-fit items-center gap-2 py-1 text-sm transition-colors",
        )}
      >
        {count > 0 ? (
          <GlobeIcon className="size-4 shrink-0 text-emerald-500" />
        ) : (
          <LoaderIcon className="size-4 shrink-0 animate-spin" />
        )}
        <span className="relative inline-block leading-none">
          {count > 0 ? (
            <span>
              已搜索&quot;{query}&quot; · 参考 {count} 个来源
            </span>
          ) : (
            <span>
              正在搜索&quot;{query}&quot;...
            </span>
          )}
          {count === 0 && (
            <span
              aria-hidden
              className="shimmer pointer-events-none absolute inset-0 motion-reduce:animate-none"
            >
              正在搜索&quot;{query}&quot;...
            </span>
          )}
        </span>
        {count > 0 && (
          <ChevronDownIcon
            className={cn(
              "size-4 shrink-0 transition-transform duration-200 ease-out",
              "group-data-[state=closed]/trigger:-rotate-90",
              "group-data-[state=open]/trigger:rotate-0",
            )}
          />
        )}
      </CollapsibleTrigger>

      {count > 0 && (
        <CollapsibleContent className="overflow-hidden text-sm data-[state=closed]:animate-collapsible-up data-[state=open]:animate-collapsible-down">
          <div className="flex flex-wrap gap-2 pt-2">
            {sources.map((s, i) => (
              <a
                key={i}
                href={s.url}
                target="_blank"
                rel="noopener noreferrer"
                title={s.snippet}
                className="inline-flex max-w-[220px] items-center gap-1 rounded-full border border-border/60 bg-muted/50 px-2.5 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              >
                <span className="font-medium">[{i + 1}]</span>
                <span className="truncate">{s.title || getDomain(s.url)}</span>
                <ExternalLinkIcon className="size-2.5 shrink-0" />
              </a>
            ))}
          </div>
        </CollapsibleContent>
      )}
    </Collapsible>
  );
};

/**
 * 注册 web_search 工具的渲染器。
 * 必须渲染在 AssistantRuntimeProvider 内部（依赖 useAui 上下文）。
 * 渲染输出为 null，仅负责把渲染器注册进 model-context。
 */
export const WebSearchToolUI = () => {
  useAssistantToolUI({
    toolName: "web_search",
    render: WebSearchRender,
    // standalone：脱离 "Used tool" 折叠组，直接内联成独立卡片
    display: "standalone",
  });
  return null;
};
