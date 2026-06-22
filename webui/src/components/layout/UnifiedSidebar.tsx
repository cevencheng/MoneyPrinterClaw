"use client";

/**
 * 一体化侧边栏（Claude Desktop 风格）
 *
 * 常驻屏幕左侧，顶部含分段胶囊切换对话/技能，中部按 pathname 动态渲染
 * ThreadList 或 SkillsList+InstallForm，底部用户头像+设置齿轮。
 * 支持折叠（w-72 ↔ w-14），折叠状态跨路由保活（AppContext）。
 */

import { usePathname } from "next/navigation";
import Link from "next/link";
import Image from "next/image";
import { useState } from "react";
import {
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
  SettingsIcon,
  UserIcon,
} from "lucide-react";
import { SegmentedToggle } from "./SegmentedToggle";
import { ThreadList } from "@/components/thread-list";
import { SkillsList } from "@/components/skills/SkillsList";
import { SkillsInstallDialog } from "@/components/skills/SkillsInstallDialog";
import { useSkillsStore } from "@/components/skills/useSkillsStore";
import { useAppContext } from "./ClientShell";
import { cn } from "@/lib/utils";

// ─── 顶栏 ──────────────────────────────────────────────────────────────────

const SidebarHeader = ({
  collapsed,
  onToggle,
}: {
  collapsed: boolean;
  onToggle: () => void;
}) => (
  <div className="shrink-0 border-b border-border/60 bg-muted/30 p-2.5">
    {collapsed ? (
      // 折叠态：仅 Logo + 展开按钮 + SegmentedToggle（纯图标）
      <div className="flex flex-col items-center gap-2">
        <Image
          src="/mpc-logo-cropped-v2.png"
          alt="MP Claw"
          width={36}
          height={36}
          className="size-9 shrink-0 rounded-md object-contain"
          priority
        />
        <button
          type="button"
          onClick={onToggle}
          title="展开侧栏"
          className="flex size-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <PanelLeftOpenIcon className="size-4" />
          <span className="sr-only">展开侧栏</span>
        </button>
        <SegmentedToggle collapsed />
      </div>
    ) : (
      // 展开态：Logo + 名称 + 折叠按钮 + SegmentedToggle
      <div className="space-y-2.5">
        <div className="flex items-center gap-2">
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
            title="折叠侧栏"
            className="flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <PanelLeftCloseIcon className="size-4" />
            <span className="sr-only">折叠侧栏</span>
          </button>
        </div>
        <SegmentedToggle />
      </div>
    )}
  </div>
);

// ─── 底部 ──────────────────────────────────────────────────────────────────

const SidebarFooter = ({ collapsed = false }: { collapsed?: boolean }) => (
  <div className="shrink-0 border-t border-border/60 bg-muted/40 px-3 py-2">
    {collapsed ? (
      // 折叠态：仅 avatar + 齿轮
      <div className="flex flex-col items-center gap-1.5">
        <div className="flex size-8 shrink-0 items-center justify-center rounded-full bg-muted-foreground/15 text-muted-foreground">
          <UserIcon className="size-4" />
        </div>
        <Link
          href="/settings"
          title="全局设置"
          className="flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <SettingsIcon className="size-4" />
        </Link>
      </div>
    ) : (
      // 展开态：avatar + 名称 + 齿轮
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <div className="flex size-8 shrink-0 items-center justify-center rounded-full bg-muted-foreground/15 text-muted-foreground">
            <UserIcon className="size-4" />
          </div>
          <div className="min-w-0 truncate text-sm text-foreground/80">本地用户</div>
        </div>
        <Link
          href="/settings"
          title="全局设置"
          className="flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <SettingsIcon className="size-4" />
        </Link>
      </div>
    )}
  </div>
);

// ─── 技能区（独立组件，避免条件性 hook 调用） ──────────────────────────────

const SkillsSection = ({
  onSelect,
  selectedName,
  collapsed = false,
}: {
  onSelect?: (name: string) => void;
  selectedName?: string | null;
  collapsed?: boolean;
}) => {
  const store = useSkillsStore();
  const [installOpen, setInstallOpen] = useState(false);

  return (
    <>
      <SkillsInstallDialog
        open={installOpen}
        onOpenChange={setInstallOpen}
        installUrl={store.installUrl}
        setInstallUrl={store.setInstallUrl}
        installZipB64={store.installZipB64}
        setInstallZipB64={store.setInstallZipB64}
        previewing={store.previewing}
        preview={store.preview}
        envGrants={store.envGrants}
        setEnvGrants={store.setEnvGrants}
        installing={store.installing}
        onPreview={store.onPreview}
        onConfirmInstall={store.onConfirmInstall}
        cancelPreview={store.cancelPreview}
        onFilePick={store.onFilePick}
        error={store.error}
        okMessage={store.okMessage}
        setError={store.setError}
        setOkMessage={store.setOkMessage}
      />
      <div className="min-h-0 flex-1 overflow-hidden">
        <SkillsList
          skills={store.skills}
          loading={store.loading}
          uninstallingName={store.uninstallingName}
          onUninstall={store.onUninstall}
          onSelect={onSelect ?? (() => {})}
          selectedName={selectedName ?? null}
          onOpenInstall={() => setInstallOpen(true)}
          collapsed={collapsed}
        />
      </div>
    </>
  );
};

// ─── 主组件 ────────────────────────────────────────────────────────────────

interface UnifiedSidebarProps {
  onSkillSelect?: (name: string) => void;
  selectedSkill?: string | null;
}

export const UnifiedSidebar = ({
  onSkillSelect,
  selectedSkill,
}: UnifiedSidebarProps = {}) => {
  const { sidebarCollapsed, setSidebarCollapsed, pagination } = useAppContext();
  const pathname = usePathname();
  const collapsed = sidebarCollapsed;
  const isSkills = pathname === "/skills" || pathname?.startsWith("/skills/");

  return (
    <aside
      className={cn(
        "shrink-0 border-r border-border/60 bg-muted/30 flex flex-col h-full transition-[width] duration-200",
        collapsed ? "w-14" : "w-72",
      )}
    >
      {/* 顶部 */}
      <SidebarHeader
        collapsed={collapsed}
        onToggle={() => setSidebarCollapsed(!collapsed)}
      />

      {/* 中部动态内容 */}
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        {isSkills ? (
          <SkillsSection
            onSelect={onSkillSelect}
            selectedName={selectedSkill}
            collapsed={collapsed}
          />
        ) : (
          <div className="flex min-h-0 flex-1 flex-col">
            <ThreadList pagination={pagination} collapsed={collapsed} />
          </div>
        )}
      </div>

      {/* 底部 */}
      <SidebarFooter collapsed={collapsed} />
    </aside>
  );
};