"use client";

/**
 * Skill 详情主区（/skills 右侧）
 *
 * Claude Desktop 风格三层 Header：
 *   1. 标题 + 顶部 Switch（macOS 一键授权网关）
 *   2. Metadata 阵列栅格（Added by / Last updated / Trigger）
 *   3. Description 描述块（圆角灰底卡）
 * 下方：SKILL.md 正文（react-markdown + remark-gfm），所有换行强制 break-word。
 */

import { useState, useEffect, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertCircleIcon,
  CalendarIcon,
  Loader2Icon,
  PackageIcon,
  UserIcon,
  ZapIcon,
} from "lucide-react";
import { apiUrl } from "@/lib/chat-adapter";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

// ─── 类型 ──────────────────────────────────────────────────────────────────

interface SkillContent {
  name: string;
  description: string;
  version: string;
  content: string;
  requires_env: string[];
  allowed_env: string[];
  source?: "user" | "project";
  installed_at?: string;
}

// ─── Markdown 组件样式 ─────────────────────────────────────────────────────
//
// 关键：每个块级元素都灌入 break-words 与 normal whitespace，
// 防止特殊符号（#、)、-）触发诡异垂直单字换行。

const mdComponents = {
  h1: ({ className, ...props }: React.HTMLProps<HTMLHeadingElement>) => (
    <h1
      className={cn(
        "mt-6 mb-3 scroll-m-20 text-xl font-semibold first:mt-0 break-words",
        className,
      )}
      {...props}
    />
  ),
  h2: ({ className, ...props }: React.HTMLProps<HTMLHeadingElement>) => (
    <h2
      className={cn(
        "mt-5 mb-2 scroll-m-20 text-lg font-semibold first:mt-0 break-words",
        className,
      )}
      {...props}
    />
  ),
  h3: ({ className, ...props }: React.HTMLProps<HTMLHeadingElement>) => (
    <h3
      className={cn(
        "mt-4 mb-2 scroll-m-20 text-base font-semibold first:mt-0 break-words",
        className,
      )}
      {...props}
    />
  ),
  h4: ({ className, ...props }: React.HTMLProps<HTMLHeadingElement>) => (
    <h4
      className={cn(
        "mt-3 mb-1 scroll-m-20 text-base font-medium first:mt-0 break-words",
        className,
      )}
      {...props}
    />
  ),
  p: ({ className, ...props }: React.HTMLProps<HTMLParagraphElement>) => (
    <p
      className={cn("mb-3 leading-relaxed last:mb-0 break-words", className)}
      {...props}
    />
  ),
  ul: ({ className, ...props }: React.HTMLAttributes<HTMLUListElement>) => (
    <ul
      className={cn("mb-3 list-disc pl-6 space-y-1 last:mb-0", className)}
      {...props}
    />
  ),
  ol: ({ className, ...props }: React.OlHTMLAttributes<HTMLOListElement>) => (
    <ol
      className={cn("mb-3 list-decimal pl-6 space-y-1 last:mb-0", className)}
      {...props}
    />
  ),
  li: ({ className, ...props }: React.LiHTMLAttributes<HTMLLIElement>) => (
    <li
      className={cn("break-words leading-relaxed", className)}
      style={{ wordBreak: "break-word", overflowWrap: "break-word" }}
      {...props}
    />
  ),
  code: ({
    className,
    inline,
    ...props
  }: React.HTMLAttributes<HTMLElement> & { inline?: boolean }) => {
    if (inline) {
      return (
        <code
          className={cn(
            "rounded bg-muted px-1.5 py-0.5 text-[0.85em] font-mono break-words",
            className,
          )}
          style={{ wordBreak: "break-word", overflowWrap: "break-word" }}
          {...props}
        />
      );
    }
    return (
      <code
        className={cn(
          "block overflow-x-auto rounded-lg bg-muted/50 p-3 text-xs font-mono",
          className,
        )}
        style={{ whiteSpace: "pre" }}
        {...props}
      />
    );
  },
  pre: ({ className, ...props }: React.HTMLAttributes<HTMLPreElement>) => (
    <pre
      className={cn(
        "mb-3 overflow-x-auto rounded-lg bg-muted/50 p-0 last:mb-0",
        className,
      )}
      {...props}
    />
  ),
  a: ({ className, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a
      className={cn(
        "text-sky-600 underline underline-offset-2 hover:text-sky-500 break-words",
        className,
      )}
      target="_blank"
      rel="noopener noreferrer"
      {...props}
    />
  ),
  blockquote: ({ className, ...props }: React.BlockquoteHTMLAttributes<HTMLQuoteElement>) => (
    <blockquote
      className={cn(
        "mb-3 border-l-2 border-border pl-4 italic text-muted-foreground last:mb-0 break-words",
        className,
      )}
      {...props}
    />
  ),
  hr: (props: React.HTMLAttributes<HTMLHRElement>) => (
    <hr className="my-5 border-border" {...props} />
  ),
  table: ({ className, ...props }: React.TableHTMLAttributes<HTMLTableElement>) => (
    <div className="mb-3 overflow-x-auto last:mb-0">
      <table
        className={cn("w-full border-collapse text-sm", className)}
        {...props}
      />
    </div>
  ),
  th: ({ className, ...props }: React.ThHTMLAttributes<HTMLTableCellElement>) => (
    <th
      className={cn(
        "border border-border bg-muted/50 px-3 py-1.5 text-left font-medium",
        className,
      )}
      {...props}
    />
  ),
  td: ({ className, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) => (
    <td
      className={cn("border border-border px-3 py-1.5", className)}
      {...props}
    />
  ),
};

// ─── 工具：格式化 installed_at（ISO → "Jun 22, 2026"） ─────────────────────

const formatInstalledAt = (iso: string): string => {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleDateString("en-US", {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return iso;
  }
};

// ─── 组件 ──────────────────────────────────────────────────────────────────

interface SkillDetailProps {
  skillName: string | null;
}

export const SkillDetail = ({ skillName }: SkillDetailProps) => {
  const [data, setData] = useState<SkillContent | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [allowedEnv, setAllowedEnv] = useState<string[]>([]);

  useEffect(() => {
    if (!skillName) {
      setData(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(apiUrl(`/api/skills/${encodeURIComponent(skillName)}/content`))
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<SkillContent>;
      })
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setAllowedEnv(d.allowed_env);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(`加载失败：${(e as Error).message}`);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [skillName]);

  // 顶部「总开关」：所有 requires_env 都已授权 → on；任一未授权 → off
  const allGranted = useMemo(() => {
    if (!data || data.requires_env.length === 0) return true;
    return data.requires_env.every((k) => allowedEnv.includes(k));
  }, [data, allowedEnv]);

  // 一键授权：全开或全关（关闭后端尚不支持 remove，只做乐观提示）
  const onToggleAll = async (checked: boolean) => {
    if (!data) return;
    if (checked) {
      // 乐观更新
      setAllowedEnv(data.requires_env);
      try {
        const r = await fetch(apiUrl("/api/skills/env-allowlist"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ keys: data.requires_env }),
        });
        if (!r.ok) {
          setAllowedEnv((prev) =>
            prev.filter((k) => data.allowed_env.includes(k)),
          );
          const detail = await r.text();
          throw new Error(detail || `HTTP ${r.status}`);
        }
      } catch (e) {
        setError(`授权失败：${(e as Error).message}`);
      }
    } else {
      // 后端 env-allowlist 是增量 add，关不掉。提示用户手改 config.toml
      setError(
        "暂不支持一键撤销授权。如需撤销请到 config.toml 编辑 skills_env_allowlist。",
      );
    }
  };

  // 单项 toggle
  const onToggleEnv = async (key: string, checked: boolean) => {
    setAllowedEnv((prev) =>
      checked ? [...prev, key] : prev.filter((k) => k !== key),
    );
    if (!checked) {
      // 同上：后端不支持 remove
      setError("暂不支持单项撤销授权，请到 config.toml 中手改。");
      return;
    }
    try {
      const r = await fetch(apiUrl("/api/skills/env-allowlist"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keys: [key] }),
      });
      if (!r.ok) {
        setAllowedEnv((prev) => prev.filter((k) => k !== key));
        const detail = await r.text();
        throw new Error(detail || `HTTP ${r.status}`);
      }
    } catch (e) {
      setError(`授权失败：${(e as Error).message}`);
    }
  };

  // ── 空态 ─────────────────────────────────────────────────────────────────
  if (!skillName) {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="text-center">
          <PackageIcon className="mx-auto size-12 text-muted-foreground/40" />
          <p className="mt-3 text-sm text-muted-foreground">
            从左侧选择一个 Skill 查看详情
          </p>
        </div>
      </div>
    );
  }

  // ── 加载中 ───────────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <Loader2Icon className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  // ── 错误态 ───────────────────────────────────────────────────────────────
  if (error && !data) {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="flex items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertCircleIcon className="size-4 shrink-0" />
          <span>{error ?? "加载失败"}</span>
        </div>
      </div>
    );
  }

  if (!data) return null;

  const addedBy =
    data.source === "user" ? "You (installed)" : "Project (built-in)";

  // ── 正常态 ───────────────────────────────────────────────────────────────
  return (
    <div className="h-full w-full overflow-y-auto">
      <div className="mx-auto w-full max-w-3xl px-8 py-8">
        {/* ── 第一层：标题 + 总开关 ──────────────────────────────────── */}
        <div className="mb-4 flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="text-3xl font-bold tracking-tight break-words">
              {data.name}
            </h1>
            {data.version && (
              <span className="mt-1.5 inline-block rounded bg-emerald-500/15 px-2 py-0.5 text-xs text-emerald-600 dark:text-emerald-400">
                v{data.version}
              </span>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-3 pt-1.5">
            <span className="text-xs text-muted-foreground">
              {data.requires_env.length === 0
                ? "无需授权"
                : allGranted
                  ? "全部已授权"
                  : "部分授权"}
            </span>
            <Switch
              checked={allGranted}
              disabled={data.requires_env.length === 0}
              onCheckedChange={onToggleAll}
            />
          </div>
        </div>

        {/* ── 第二层：Metadata 阵列栅格 ────────────────────────────── */}
        <div className="mb-4 flex flex-wrap items-center gap-x-5 gap-y-1.5 text-xs text-muted-foreground">
          <div className="flex items-center gap-1.5">
            <UserIcon className="size-3.5" />
            <span>Added by:</span>
            <span className="text-foreground/70">{addedBy}</span>
          </div>
          <div className="flex items-center gap-1.5">
            <CalendarIcon className="size-3.5" />
            <span>Last updated:</span>
            <span className="text-foreground/70">
              {formatInstalledAt(data.installed_at ?? "")}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <ZapIcon className="size-3.5" />
            <span>Trigger:</span>
            <span className="text-foreground/70">Slash command + auto</span>
          </div>
        </div>

        {/* ── 第三层：Description 卡片 ────────────────────────────── */}
        {data.description && (
          <div className="mb-6 rounded-xl border border-slate-200/40 bg-slate-50 p-3 text-sm text-slate-600 dark:border-slate-700/40 dark:bg-slate-900/60 dark:text-slate-300">
            <p className="break-words leading-relaxed">{data.description}</p>
          </div>
        )}

        {/* error 浮条（已加载情况下的非阻断错误） */}
        {error && (
          <div className="mb-4 flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-2.5 text-sm text-destructive">
            <AlertCircleIcon className="size-4 shrink-0 mt-0.5" />
            <span className="flex-1 break-words">{error}</span>
            <button
              onClick={() => setError(null)}
              className="text-xs underline"
            >
              关闭
            </button>
          </div>
        )}

        {/* ── 每个 env 的细颗粒开关（如有 requires_env） ────────── */}
        {data.requires_env.length > 0 && (
          <div className="mb-6 rounded-xl border border-border/60 p-3">
            <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              环境变量授权
            </div>
            <div className="space-y-1.5">
              {data.requires_env.map((key) => (
                <label
                  key={key}
                  className="flex items-center justify-between gap-3 rounded-md px-2 py-1.5 transition-colors hover:bg-muted/50"
                >
                  <code className="break-all text-sm font-mono">{key}</code>
                  <Switch
                    checked={allowedEnv.includes(key)}
                    onCheckedChange={(checked) => onToggleEnv(key, checked)}
                  />
                </label>
              ))}
            </div>
          </div>
        )}

        {/* ── SKILL.md 正文 ────────────────────────────────────── */}
        <div
          className="text-sm leading-relaxed text-foreground/90"
          style={{
            wordBreak: "break-word",
            overflowWrap: "break-word",
          }}
        >
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={mdComponents}
          >
            {data.content}
          </ReactMarkdown>
        </div>
      </div>
    </div>
  );
};