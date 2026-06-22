"use client";

/**
 * 分段胶囊切换：对话 ↔ 技能
 *
 * Claude Desktop / macOS 风格的 segmented control,内嵌在 UnifiedSidebar 顶部。
 * 活跃项用白色（深色模式 slate-800）card 衬底 + shadow,未活跃项保持深灰优雅色阶。
 */

import { usePathname } from "next/navigation";
import Link from "next/link";
import { MessageCircleIcon, SparklesIcon } from "lucide-react";
import { cn } from "@/lib/utils";

const ITEMS = [
  { href: "/", label: "对话", Icon: MessageCircleIcon },
  { href: "/skills", label: "技能", Icon: SparklesIcon },
] as const;

export const SegmentedToggle = ({ collapsed = false }: { collapsed?: boolean }) => {
  const pathname = usePathname();
  // 把 /settings 视作 "对话" 侧（齿轮入口虽来自 /,但 settings 自己不属于 segmented 二选一）
  // 仅 /skills 算技能 active,其余都给 / 高亮
  const isSkills = pathname === "/skills" || pathname?.startsWith("/skills/");

  if (collapsed) {
    // 折叠态：竖向两个图标按钮
    return (
      <div className="flex flex-col gap-1 rounded-xl bg-slate-200/50 dark:bg-slate-800/50 p-1">
        {ITEMS.map(({ href, label, Icon }) => {
          const active = href === "/skills" ? isSkills : !isSkills;
          return (
            <Link
              key={href}
              href={href}
              title={label}
              className={cn(
                "flex items-center justify-center rounded-lg p-1.5 transition-colors",
                active
                  ? "bg-white dark:bg-slate-800 shadow-sm text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              <Icon className="size-4" />
            </Link>
          );
        })}
      </div>
    );
  }

  return (
    <div className="flex w-full items-center rounded-xl bg-slate-200/50 dark:bg-slate-800/50 p-1">
      {ITEMS.map(({ href, label, Icon }) => {
        const active = href === "/skills" ? isSkills : !isSkills;
        return (
          <Link
            key={href}
            href={href}
            className={cn(
              "flex flex-1 items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-sm transition-colors",
              active
                ? "bg-white dark:bg-slate-800 shadow-sm font-medium text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            <Icon className="size-4" />
            <span>{label}</span>
          </Link>
        );
      })}
    </div>
  );
};