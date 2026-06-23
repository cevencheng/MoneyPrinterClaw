"use client";

/**
 * Skills 状态管理 hook
 *
 * 从 SkillsTab 提取所有 useState + 方法（refreshList / onPreview / onConfirmInstall / onUninstall / onFilePick）。
 * 在 UnifiedSidebar 的 /skills 路径下调用。
 */

import { apiUrl } from "@/lib/chat-adapter";
import { useCallback, useEffect, useState } from "react";

// ─── 类型 ──────────────────────────────────────────────────────────────────

export type SkillItem = {
  name: string;
  description: string;
  version: string;
  requires_env: string[];
  location: string;
  source: "project" | "user";
  removable: boolean;
};

export type ScriptPreview = {
  path: string;
  head: string;
  total_lines: number;
};

export type PreviewResult = {
  name: string;
  description: string;
  version: string;
  requires_env: string[];
  file_count: number;
  script_count: number;
  scripts_preview: ScriptPreview[];
  source_type: "github" | "zip";
  source_ref: string;
};

// ─── 工具 ──────────────────────────────────────────────────────────────────

async function fileToBase64(file: File): Promise<string> {
  const buf = await file.arrayBuffer();
  let binary = "";
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.byteLength; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

// ─── Hook ──────────────────────────────────────────────────────────────────

export function useSkillsStore() {
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

  // ── 拉列表 ─────────────────────────────────────────────────────────────
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

  // ── 预览 ───────────────────────────────────────────────────────────────
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
      const initial: Record<string, boolean> = {};
      for (const k of data.requires_env) initial[k] = true;
      setEnvGrants(initial);
    } catch (e) {
      setError(`预览失败：${(e as Error).message}`);
    } finally {
      setPreviewing(false);
    }
  };

  // ── 确认安装 ───────────────────────────────────────────────────────────
  const onConfirmInstall = async () => {
    if (!preview) return;
    setError(null);
    setOkMessage(null);
    setInstalling(true);
    try {
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
      setOkMessage(
        `已安装 ${preview.name}${grantKeys.length > 0 ? `,授权 ${grantKeys.length} 个环境变量` : ""}`,
      );
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

  // ── 卸载 ───────────────────────────────────────────────────────────────
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

  // ── 文件选择 ───────────────────────────────────────────────────────────
  const onFilePick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".zip")) {
      setError("仅支持 .zip 文件");
      return;
    }
    setError(null);
    setInstallUrl("");
    try {
      const b64 = await fileToBase64(file);
      setInstallZipB64({ name: file.name, b64 });
    } catch (err) {
      setError(`读取文件失败：${(err as Error).message}`);
    }
  };

  const cancelPreview = () => {
    setPreview(null);
    setEnvGrants({});
  };

  return {
    // 列表
    skills,
    loading,
    error,
    okMessage,
    refreshList,
    setError,
    setOkMessage,
    // 安装
    installUrl,
    setInstallUrl,
    installZipB64,
    setInstallZipB64,
    previewing,
    preview,
    envGrants,
    setEnvGrants,
    installing,
    uninstallingName,
    onPreview,
    onConfirmInstall,
    onUninstall,
    onFilePick,
    cancelPreview,
  };
}