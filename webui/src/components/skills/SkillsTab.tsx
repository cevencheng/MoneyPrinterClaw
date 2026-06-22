"use client";

/**
 * Skills 管理面板（设置页内置 tab）
 *
 * 渲染在 /settings 页的 Skills 区块。一个组件搞定：
 * - 列出当前已安装的 skill（project/user 区分）
 * - 粘贴 GitHub URL 或选本地 .zip 安装
 * - 装前预览审查（SKILL.md 摘要 + 脚本清单 + requires-env 逐项确认）
 * - user 级 skill 可卸载（带 .install.json 元数据的目录）
 *
 * 调 6 个后端端点（/api/skills/*）。不引 react-query/swr,沿用 settings page 风格的
 * 原生 fetch + useState + try/catch。
 */

import { apiUrl } from "@/lib/chat-adapter";
import { Button } from "@/components/ui/button";
import {
  AlertCircleIcon,
  CheckCircle2Icon,
  Loader2Icon,
  PackageIcon,
  RefreshCwIcon,
  ShieldIcon,
  Trash2Icon,
  UploadCloudIcon,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";

// ─── 类型 ────────────────────────────────────────────────────────────────

interface SkillItem {
  name: string;
  description: string;
  version: string;
  requires_env: string[];
  location: string;
  source: "project" | "user";
  removable: boolean;
}

interface ScriptPreview {
  path: string;
  head: string;
  total_lines: number;
}

interface PreviewResult {
  name: string;
  description: string;
  version: string;
  requires_env: string[];
  file_count: number;
  script_count: number;
  scripts_preview: ScriptPreview[];
  source_type: "github" | "zip";
  source_ref: string;
}

// ─── 工具 ────────────────────────────────────────────────────────────────

async function fileToBase64(file: File): Promise<string> {
  const buf = await file.arrayBuffer();
  let binary = "";
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.byteLength; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

// ─── 主组件 ──────────────────────────────────────────────────────────────

export function SkillsTab() {
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [okMessage, setOkMessage] = useState<string | null>(null);

  // 安装表单状态
  const [installUrl, setInstallUrl] = useState("");
  const [installZipB64, setInstallZipB64] = useState<{ name: string; b64: string } | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [envGrants, setEnvGrants] = useState<Record<string, boolean>>({});
  const [installing, setInstalling] = useState(false);
  const [uninstallingName, setUninstallingName] = useState<string | null>(null);

  // ── 拉列表 ───────────────────────────────────────────────────────────
  const refreshList = useCallback(async () => {
    try {
      const r = await fetch(apiUrl("/api/skills"));
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = (await r.json()) as SkillItem[];
      setSkills(data);
      setError(null);
    } catch (e) {
      setError(`加载失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refreshList();
  }, [refreshList]);

  // ── 预览 ─────────────────────────────────────────────────────────────
  const onPreview = async () => {
    setError(null);
    setOkMessage(null);
    setPreview(null);
    setPreviewing(true);
    try {
      const body: Record<string, string> = {};
      if (installUrl.trim()) body.url = installUrl.trim();
      else if (installZipB64) body.zip_b64 = installZipB64.b64;
      else {
        throw new Error("请粘贴 GitHub URL 或选择 .zip 文件");
      }
      const r = await fetch(apiUrl("/api/skills/preview"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const detail = await r.text();
        throw new Error(detail || `HTTP ${r.status}`);
      }
      const data = (await r.json()) as PreviewResult;
      setPreview(data);
      // 默认全部勾选 requires-env（保留地雷 2 渐进同意：用户可去掉某项不授权）
      const initial: Record<string, boolean> = {};
      for (const k of data.requires_env) initial[k] = true;
      setEnvGrants(initial);
    } catch (e) {
      setError(`预览失败：${(e as Error).message}`);
    } finally {
      setPreviewing(false);
    }
  };

  // ── 确认安装 ─────────────────────────────────────────────────────────
  const onConfirmInstall = async () => {
    if (!preview) return;
    setError(null);
    setOkMessage(null);
    setInstalling(true);
    try {
      // 1. 装 skill
      const body: Record<string, string> = {};
      if (installUrl.trim()) body.url = installUrl.trim();
      else if (installZipB64) body.zip_b64 = installZipB64.b64;
      else throw new Error("源已丢失,请重新选择");
      const r = await fetch(apiUrl("/api/skills/install"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const detail = await r.text();
        throw new Error(detail || `HTTP ${r.status}`);
      }
      // 2. 增量授权用户勾选的 env keys
      const grantKeys = Object.entries(envGrants)
        .filter(([, v]) => v)
        .map(([k]) => k);
      if (grantKeys.length > 0) {
        const r2 = await fetch(apiUrl("/api/skills/env-allowlist"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ keys: grantKeys }),
        });
        if (!r2.ok) {
          const detail = await r2.text();
          throw new Error(`安装成功但 env 授权失败：${detail || r2.status}`);
        }
      }
      setOkMessage(`已安装 ${preview.name}${grantKeys.length > 0 ? `,授权 ${grantKeys.length} 个环境变量` : ""}`);
      setInstallUrl("");
      setInstallZipB64(null);
      setPreview(null);
      setEnvGrants({});
      await refreshList();
    } catch (e) {
      setError(`安装失败：${(e as Error).message}`);
    } finally {
      setInstalling(false);
    }
  };

  // ── 卸载 ─────────────────────────────────────────────────────────────
  const onUninstall = async (name: string) => {
    if (!confirm(`确认卸载 skill "${name}"?该操作不可撤销。`)) return;
    setUninstallingName(name);
    setError(null);
    setOkMessage(null);
    try {
      const r = await fetch(apiUrl(`/api/skills/${encodeURIComponent(name)}`), {
        method: "DELETE",
      });
      if (!r.ok) {
        const detail = await r.text();
        throw new Error(detail || `HTTP ${r.status}`);
      }
      setOkMessage(`已卸载 ${name}`);
      await refreshList();
    } catch (e) {
      setError(`卸载失败：${(e as Error).message}`);
    } finally {
      setUninstallingName(null);
    }
  };

  // ── 文件选择 ─────────────────────────────────────────────────────────
  const onFilePick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".zip")) {
      setError("仅支持 .zip 文件");
      return;
    }
    setError(null);
    setInstallUrl(""); // 互斥
    try {
      const b64 = await fileToBase64(file);
      setInstallZipB64({ name: file.name, b64 });
    } catch (err) {
      setError(`读取文件失败：${(err as Error).message}`);
    }
  };

  // ── 渲染 ─────────────────────────────────────────────────────────────
  return (
    <div className="space-y-6">
      {/* 状态条 */}
      {error && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          <AlertCircleIcon className="size-4 shrink-0 mt-0.5" />
          <span className="flex-1 break-all">{error}</span>
          <button onClick={() => setError(null)} className="text-xs underline">关闭</button>
        </div>
      )}
      {okMessage && (
        <div className="flex items-start gap-2 rounded-lg border border-emerald-500/40 bg-emerald-500/5 p-3 text-sm text-emerald-700 dark:text-emerald-400">
          <CheckCircle2Icon className="size-4 shrink-0 mt-0.5" />
          <span className="flex-1">{okMessage}</span>
          <button onClick={() => setOkMessage(null)} className="text-xs underline">关闭</button>
        </div>
      )}

      {/* 已安装列表 */}
      <section>
        <div className="mb-3 flex items-center gap-2">
          <h2 className="text-lg font-semibold">已安装 Skills</h2>
          <span className="text-sm text-muted-foreground">({skills.length})</span>
          <Button
            onClick={refreshList}
            variant="ghost"
            size="sm"
            className="ml-auto"
            disabled={loading}
          >
            <RefreshCwIcon className={`size-3.5 ${loading ? "animate-spin" : ""}`} />
            刷新
          </Button>
        </div>
        {loading ? (
          <div className="flex items-center justify-center gap-2 py-8 text-muted-foreground">
            <Loader2Icon className="size-4 animate-spin" />
            加载中…
          </div>
        ) : skills.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border/60 p-6 text-center text-sm text-muted-foreground">
            尚未发现任何 skill。从下方安装一个开始。
          </div>
        ) : (
          <div className="grid gap-2">
            {skills.map((s) => (
              <div
                key={s.name}
                className="flex items-start gap-3 rounded-lg border border-border/60 bg-muted/20 p-3"
              >
                <PackageIcon className="size-5 shrink-0 mt-0.5 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{s.name}</span>
                    <span
                      className={`rounded px-1.5 py-0.5 text-[10px] ${
                        s.source === "user"
                          ? "bg-sky-500/15 text-sky-600 dark:text-sky-400"
                          : "bg-muted text-muted-foreground"
                      }`}
                    >
                      {s.source === "user" ? "已安装" : "内置"}
                    </span>
                    {s.version && (
                      <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] text-emerald-600 dark:text-emerald-400">
                        v{s.version}
                      </span>
                    )}
                  </div>
                  <p className="mt-0.5 text-sm text-muted-foreground line-clamp-2">{s.description}</p>
                  {s.requires_env.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {s.requires_env.map((k) => (
                        <span
                          key={k}
                          className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-400"
                        >
                          🔑 {k}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                {s.removable && (
                  <button
                    onClick={() => onUninstall(s.name)}
                    disabled={uninstallingName === s.name}
                    className="shrink-0 rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive disabled:opacity-50"
                    aria-label={`卸载 ${s.name}`}
                  >
                    {uninstallingName === s.name ? (
                      <Loader2Icon className="size-4 animate-spin" />
                    ) : (
                      <Trash2Icon className="size-4" />
                    )}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* 安装表单 */}
      <section>
        <h2 className="mb-3 text-lg font-semibold">安装新 Skill</h2>

        {/* 提示安全注意 */}
        <div className="mb-3 flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-xs text-amber-700 dark:text-amber-400">
          <ShieldIcon className="size-4 shrink-0 mt-0.5" />
          <div>
            <div className="font-medium">Skills 是可执行代码,请只装可信源</div>
            <div className="mt-0.5">
              安装前可在「预览」中查看脚本前 50 行 + 声明的环境变量。绝不要装来源不明的 skill;
              即使是公共 GitHub 仓,也请核对 Star 数、维护者背景再装。
            </div>
          </div>
        </div>

        <div className="space-y-3 rounded-lg border border-border/60 bg-muted/20 p-4">
          <div>
            <label className="mb-1 block text-sm font-medium">
              GitHub 子目录 URL
            </label>
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
          <div className="mt-4 space-y-3 rounded-lg border-2 border-sky-500/40 bg-sky-500/5 p-4">
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
                <div className="space-y-2">
                  {preview.scripts_preview.map((sc) => (
                    <details key={sc.path} className="rounded border border-border/60 bg-background">
                      <summary className="cursor-pointer px-2 py-1.5 text-xs font-mono hover:bg-muted">
                        {sc.path} ({sc.total_lines} lines)
                      </summary>
                      <pre className="max-h-64 overflow-auto border-t border-border/40 bg-muted/30 p-2 text-[11px] leading-tight">
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
                    取消勾选 = 该变量不会传给 skill 脚本（脚本里读到的是空字符串）
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

            {/* 确认按钮 */}
            <div className="flex justify-end gap-2 pt-1">
              <Button
                onClick={() => {
                  setPreview(null);
                  setEnvGrants({});
                }}
                variant="outline"
                size="sm"
                disabled={installing}
              >
                取消
              </Button>
              <Button onClick={onConfirmInstall} disabled={installing} size="sm">
                {installing ? (
                  <>
                    <Loader2Icon className="size-4 animate-spin" />
                    安装中…
                  </>
                ) : (
                  "确认安装"
                )}
              </Button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
