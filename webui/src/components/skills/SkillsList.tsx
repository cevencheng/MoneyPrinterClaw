"use client";

/**
 * 侧栏内已安装 Skills 列表
 *
 * 渲染在 UnifiedSidebar 中部（pathname === "/skills" 时显示）。
 * Claude 风格：顶部紧凑搜索框 + "+" 按钮（唤醒安装 Dialog），
 * 下方列表项点击切换右侧详情视图，hover 操作出现卸载按钮。
 *
 * 折叠态（collapsed）：
 * - 搜索框 hidden，仅 "+" 按钮 mx-auto 居中
 * - 列表项只保留图标 justify-center p-2，文字 / Badge / 卸载全部 hidden
 */

import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Loader2Icon,
  PackageIcon,
  PlusIcon,
  SearchIcon,
  Trash2Icon,
} from "lucide-react";
import type { SkillItem } from "./useSkillsStore";
import { cn } from "@/lib/utils";

interface SkillsListProps {
  skills: SkillItem[];
  loading: boolean;
  uninstallingName: string | null;
  onUninstall: (name: string) => void;
  onSelect: (name: string) => void;
  selectedName: string | null;
  onOpenInstall: () => void;
  collapsed?: boolean;
}

export const SkillsList = ({
  skills,
  loading,
  uninstallingName,
  onUninstall,
  onSelect,
  selectedName,
  onOpenInstall,
  collapsed = false,
}: SkillsListProps) => {
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return skills;
    return skills.filter(
      (s) =>
        s.name.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q),
    );
  }, [skills, query]);

  // 折叠态：与对话 tab 对齐——仅渲染 "+" 按钮，列表整块不渲染
  if (collapsed) {
    return (
      <section className="flex h-full min-h-0 flex-col">
        <div className="shrink-0 flex justify-center px-2 pt-3 pb-2">
          <Button
            onClick={onOpenInstall}
            size="sm"
            variant="outline"
            className="size-7 p-0"
            aria-label="安装新 Skill"
            title="安装新 Skill"
          >
            <PlusIcon className="size-3.5" strokeWidth={2.5} />
          </Button>
        </div>
      </section>
    );
  }

  return (
    <section className="flex h-full min-h-0 flex-col">
      {/* ── 顶部：搜索 + 新建 ───────────────────────────────────────── */}
      <div className="shrink-0 px-3 pt-3 pb-2">
        <div className="flex items-center gap-1.5">
          <div className="relative flex-1">
            <SearchIcon className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索 skill"
              className="w-full rounded-lg border border-border/60 bg-background/50 py-1.5 pl-7 pr-2 text-xs transition-colors focus:border-foreground/30 focus:outline-none focus:ring-1 focus:ring-foreground/10"
            />
          </div>
          <Button
            onClick={onOpenInstall}
            size="sm"
            variant="outline"
            className="size-7 shrink-0 p-0"
            aria-label="安装新 Skill"
            title="安装新 Skill"
          >
            <PlusIcon className="size-3.5" strokeWidth={2.5} />
          </Button>
        </div>
      </div>

      {/* ── 列表（滚动） ───────────────────────────────────────────── */}
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {loading ? (
          <div className="flex items-center justify-center gap-2 py-6 text-xs text-muted-foreground">
            <Loader2Icon className="size-3 animate-spin" />
            加载中…
          </div>
        ) : filtered.length === 0 ? (
          <div className="rounded-md border border-dashed border-border/60 p-4 text-center text-xs text-muted-foreground">
            {query ? "无匹配的 skill" : "暂无 skill，点击 + 安装一个"}
          </div>
        ) : (
          <div className="flex flex-col gap-0.5">
            {filtered.map((s) => {
              const active = s.name === selectedName;
              return (
                <div
                  key={s.name}
                  className={cn(
                    "group relative flex items-start gap-2 rounded-md border px-2 py-1.5 text-sm transition-colors",
                    active
                      ? "border-blue-500/30 bg-blue-500/10 dark:border-blue-400/40 dark:bg-blue-400/15"
                      : "border-transparent hover:border-border/60 hover:bg-slate-200/50 dark:hover:bg-slate-800/50",
                  )}
                >
                  <button
                    type="button"
                    onClick={() => onSelect(s.name)}
                    className="flex min-w-0 flex-1 items-start gap-2 text-left"
                  >
                    <PackageIcon
                      className={cn(
                        "mt-0.5 size-3.5 shrink-0",
                        active
                          ? "text-blue-600 dark:text-blue-400"
                          : "text-muted-foreground",
                      )}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span
                          className={cn(
                            "truncate",
                            active
                              ? "font-semibold text-foreground"
                              : "font-medium text-foreground/90",
                          )}
                        >
                          {s.name}
                        </span>
                        {s.source === "user" && (
                          <span className="shrink-0 rounded bg-sky-500/15 px-1 py-px text-[9px] text-sky-600 dark:text-sky-400">
                            U
                          </span>
                        )}
                      </div>
                      <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
                        {s.description}
                      </p>
                    </div>
                  </button>
                  {s.removable && (
                    <button
                      onClick={() => onUninstall(s.name)}
                      disabled={uninstallingName === s.name}
                      className="shrink-0 rounded p-1 text-muted-foreground opacity-0 transition-opacity hover:bg-destructive/10 hover:text-destructive group-hover:opacity-100 disabled:opacity-50"
                      aria-label={`卸载 ${s.name}`}
                    >
                      {uninstallingName === s.name ? (
                        <Loader2Icon className="size-3 animate-spin" />
                      ) : (
                        <Trash2Icon className="size-3" />
                      )}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
};