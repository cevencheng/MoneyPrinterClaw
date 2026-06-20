"use client";

import type { FC } from "react";
import { useAui, useAuiState } from "@assistant-ui/react";
import type { SyntaxHighlighterProps as AUIProps } from "@assistant-ui/react-markdown";
import { cn } from "@/lib/utils";
import { useEffect, useRef, useState } from "react";
import hljs from "highlight.js";

export type HighlighterProps = Pick<AUIProps, "language" | "code"> &
  Partial<Pick<AUIProps, "node" | "components">> & {
    className?: string;
    style?: React.CSSProperties;
  };

const containerClassName =
  "aui-shiki-base [&_pre]:border-border/50 [&_pre]:bg-muted/30! [&_pre]:overflow-x-auto [&_pre]:rounded-t-none [&_pre]:rounded-b-xl [&_pre]:border [&_pre]:border-t-0 [&_pre]:p-3.5 [&_pre]:text-[13px] [&_pre]:leading-relaxed";

/**
 * 代码高亮组件（基于 highlight.js，无 WASM 依赖）
 *
 * 流式输出时显示纯文本，完成后高亮渲染，避免布局跳动。
 */
export const SyntaxHighlighter: FC<HighlighterProps> = ({
  code,
  language,
  className,
  style,
}) => {
  const aui = useAui();
  const hasPart = aui.part.source !== null;
  const isStreaming = useAuiState(
    (s) => hasPart && s.part.status.type === "running",
  );
  const trimmed = code.trim();
  const codeRef = useRef<HTMLElement>(null);
  const [highlighted, setHighlighted] = useState("");

  useEffect(() => {
    if (!isStreaming && trimmed) {
      try {
        const result = language && hljs.getLanguage(language)
          ? hljs.highlight(trimmed, { language }).value
          : hljs.highlightAuto(trimmed).value;
        setHighlighted(result);
      } catch {
        setHighlighted(trimmed);
      }
    }
  }, [isStreaming, trimmed, language]);

  return (
    <div className={cn(containerClassName, className)} style={style}>
      {isStreaming || !highlighted ? (
        <pre>
          <code>{trimmed}</code>
        </pre>
      ) : (
        <pre>
          <code
            ref={codeRef}
            className={language ? `language-${language}` : ""}
            dangerouslySetInnerHTML={{ __html: highlighted }}
          />
        </pre>
      )}
    </div>
  );
};

SyntaxHighlighter.displayName = "SyntaxHighlighter";
