"use client";

/**
 * 安装 Skill 的 Dialog 弹窗
 *
 * 对应 sidebar 顶部 "+" 按钮点击后唤醒。内部封装：
 * - GitHub URL 输入 / Zip 文件选择（互斥）
 * - 远程预览 + 脚本清单 + requires-env 逐项确认
 * - 确认安装 / 取消
 */

import {
  AlertCircleIcon,
  CheckCircle2Icon,
  Loader2Icon,
  PackageIcon,
  ShieldIcon,
  UploadCloudIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { PreviewResult } from "./useSkillsStore";

interface SkillsInstallDialogProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  // 安装表单状态（从 useSkillsStore 接入）
  installUrl: string;
  setInstallUrl: (v: string) => void;
  installZipB64: { name: string; b64: string } | null;
  setInstallZipB64: (v: { name: string; b64: string } | null) => void;
  previewing: boolean;
  preview: PreviewResult | null;
  envGrants: Record<string, boolean>;
  setEnvGrants: React.Dispatch<React.SetStateAction<Record<string, boolean>>>;
  installing: boolean;
  onPreview: () => void;
  onConfirmInstall: () => void;
  cancelPreview: () => void;
  onFilePick: (e: React.ChangeEvent<HTMLInputElement>) => void;
  error: string | null;
  okMessage: string | null;
  setError: (v: string | null) => void;
  setOkMessage: (v: string | null) => void;
}

export const SkillsInstallDialog = ({
  open,
  onOpenChange,
  installUrl,
  setInstallUrl,
  installZipB64,
  setInstallZipB64,
  previewing,
  preview,
  envGrants,
  setEnvGrants,
  installing,
  onPreview,
  onConfirmInstall,
  cancelPreview,
  onFilePick,
  error,
  okMessage,
  setError,
  setOkMessage,
}: SkillsInstallDialogProps) => {
  // 关弹窗时自动重置状态
  const handleClose = () => {
    setError(null);
    setOkMessage(null);
    cancelPreview();
    onOpenChange(false);
  };

  // 安装成功 → 关弹窗
  const handleInstall = async () => {
    await onConfirmInstall();
    // onConfirmInstall 内部成功后只更新 okMessage，
    // 我们监控 okMessage 变化来关窗（或简单地在调用后判断）
    handleClose();
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-lg max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>安装新 Skill</DialogTitle>
          <DialogDescription>
            粘贴 GitHub 子目录 URL 或上传 .zip 安装包
          </DialogDescription>
        </DialogHeader>

        {/* 安全提示 */}
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-xs text-amber-700 dark:text-amber-400">
          <ShieldIcon className="size-4 shrink-0 mt-0.5" />
          <div>
            <div className="font-medium">Skills 是可执行代码，请只装可信源</div>
            <div className="mt-0.5">
              安装前可在「预览」中查看脚本前 50 行 + 声明的环境变量。
            </div>
          </div>
        </div>

        {/* 状态条 */}
        {error && (
          <div className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-2.5 text-sm text-destructive">
            <AlertCircleIcon className="size-4 shrink-0 mt-0.5" />
            <span className="flex-1 break-all">{error}</span>
            <button onClick={() => setError(null)} className="text-xs underline">关闭</button>
          </div>
        )}
        {okMessage && (
          <div className="flex items-start gap-2 rounded-md border border-emerald-500/40 bg-emerald-500/5 p-2.5 text-sm text-emerald-700 dark:text-emerald-400">
            <CheckCircle2Icon className="size-4 shrink-0 mt-0.5" />
            <span className="flex-1">{okMessage}</span>
            <button onClick={() => setOkMessage(null)} className="text-xs underline">关闭</button>
          </div>
        )}

        {/* 表单 */}
        <div className="space-y-3">
          <div>
            <label className="mb-1 block text-sm font-medium">GitHub 子目录 URL</label>
            <input
              type="text"
              value={installUrl}
              onChange={(e) => {
                setInstallUrl(e.target.value);
                if (e.target.value) setInstallZipB64(null);
              }}
              placeholder="https://github.com/anthropics/skills/tree/main/skills/pdf"
              className="w-full rounded-md border border-border/60 bg-background px-3 py-2 text-sm transition-colors focus:border-foreground/30 focus:outline-none focus:ring-2 focus:ring-foreground/10"
              disabled={previewing || installing}
            />
          </div>

          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span>或</span>
            <label className="inline-flex cursor-pointer items-center gap-1 rounded-md border border-border/60 bg-background px-3 py-1.5 transition-colors hover:bg-muted">
              <UploadCloudIcon className="size-3.5" />
              选择 .zip 包
              <input
                type="file"
                accept=".zip"
                className="hidden"
                onChange={onFilePick}
                disabled={previewing || installing}
              />
            </label>
            {installZipB64 && (
              <span className="rounded bg-muted px-2 py-0.5">{installZipB64.name}</span>
            )}
          </div>

          <Button
            onClick={onPreview}
            disabled={previewing || installing || (!installUrl && !installZipB64)}
            variant="outline"
            size="sm"
            className="w-full"
          >
            {previewing ? (
              <>
                <Loader2Icon className="size-4 animate-spin" />
                预览中…
              </>
            ) : (
              "预览"
            )}
          </Button>
        </div>

        {/* 预览结果 */}
        {preview && (
          <div className="space-y-3 rounded-lg border-2 border-sky-500/40 bg-sky-500/5 p-4">
            <div className="flex items-center gap-2">
              <PackageIcon className="size-5 text-sky-600 dark:text-sky-400" />
              <span className="text-lg font-semibold">{preview.name}</span>
              {preview.version && (
                <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] text-emerald-600 dark:text-emerald-400">
                  v{preview.version}
                </span>
              )}
            </div>
            <p className="text-sm text-muted-foreground">{preview.description}</p>
            <div className="text-xs text-muted-foreground">
              {preview.file_count} 个文件 · {preview.script_count} 个脚本 · {preview.source_type}
            </div>

            {/* 脚本清单 */}
            {preview.scripts_preview.length > 0 && (
              <div>
                <div className="mb-1 text-sm font-medium">脚本预览（前 50 行）</div>
                <div className="space-y-2 max-h-48 overflow-y-auto">
                  {preview.scripts_preview.map((sc) => (
                    <details key={sc.path} className="rounded border border-border/60 bg-background">
                      <summary className="cursor-pointer px-2 py-1.5 text-xs font-mono hover:bg-muted">
                        {sc.path} ({sc.total_lines} lines)
                      </summary>
                      <pre className="max-h-32 overflow-auto border-t border-border/40 bg-muted/30 p-2 text-[11px] leading-tight">
                        {sc.head}
                      </pre>
                    </details>
                  ))}
                </div>
              </div>
            )}

            {/* requires-env 逐项确认 */}
            {preview.requires_env.length > 0 && (
              <div>
                <div className="mb-1 flex items-center gap-1.5 text-sm font-medium">
                  <ShieldIcon className="size-3.5" />
                  需要的环境变量
                </div>
                <div className="space-y-1.5 rounded bg-amber-500/5 p-2">
                  <div className="text-xs text-amber-700 dark:text-amber-400">
                    取消勾选 = 该变量不会传给 skill 脚本
                  </div>
                  {preview.requires_env.map((k) => (
                    <label key={k} className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={envGrants[k] ?? false}
                        onChange={(e) =>
                          setEnvGrants((prev) => ({ ...prev, [k]: e.target.checked }))
                        }
                      />
                      <span className="font-mono">{k}</span>
                    </label>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        <DialogFooter>
          <Button
            variant="outline"
            onClick={handleClose}
            disabled={installing}
          >
            取消
          </Button>
          {preview && (
            <Button onClick={handleInstall} disabled={installing}>
              {installing ? (
                <>
                  <Loader2Icon className="size-4 animate-spin" />
                  安装中…
                </>
              ) : (
                "确认安装"
              )}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};