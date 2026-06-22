"use client";

/**
 * 全局配置页 — /settings
 *
 * 三段表单（LLM / 联网搜索 / 视频素材）+ 受控 input + 保存按钮。
 * 不引 react-hook-form/zod —— 字段总共 7 个,手写就够用。
 *
 * Key 字段交互:
 *   后端 GET 直接返回 key 明文。
 *   前端把 key 字段当作密码输入框处理：默认 type="password" 显示星号，
 *   点击右侧小眼睛切换为 type="text" 查看原文。
 *
 * 保存策略:
 *   POST 写回 config.toml(原子 .tmp + replace)+ 后端即时刷新内存单例 → 弹"已保存,即时生效"。
 *   无需重启:bocha/pexels/pixabay 消费点本就每次读 settings.*;
 *   openai_api_key/model_* 由节点每次调用按需构造 ChatOpenAI,下一次对话即用新值。
 */

import { apiUrl } from "@/lib/chat-adapter";
import { Button } from "@/components/ui/button";
import { SkillsTab } from "@/components/skills/SkillsTab";
import {
  AlertCircleIcon,
  ArrowLeftIcon,
  CheckCircle2Icon,
  EyeIcon,
  EyeOffIcon,
  Loader2Icon,
  SettingsIcon,
  PackageIcon,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

// ─── 字段元数据 ────────────────────────────────────────────────────────────

interface FieldMeta {
  group: "llm" | "search" | "media";
  is_key: boolean;
  value: string | number;
}
type FieldsResponse = { fields: Record<string, FieldMeta> };

interface FieldDef {
  group: "llm" | "search" | "media";
  name: string;
  label: string;
  type: "text" | "password" | "number";
  hint: string;
  // 可选：申请入口链接，渲染在 label 右侧「点击获取」
  link?: { url: string; text: string };
  // 可选：标记为推荐素材源
  recommended?: boolean;
}

const FIELD_DEFS: FieldDef[] = [
  // LLM
  { group: "llm", name: "openai_api_key", label: "API Key", type: "password",
    hint: "DeepSeek / OpenAI 兼容 API 的密钥（必填）" },
  { group: "llm", name: "openai_base_url", label: "Base URL", type: "text",
    hint: "DeepSeek 填 https://api.deepseek.com；OpenAI 留空走官方" },
  { group: "llm", name: "model_name", label: "模型名", type: "text",
    hint: "如 deepseek-chat / deepseek-v4-flash / gpt-4o" },
  { group: "llm", name: "model_temperature", label: "温度", type: "number",
    hint: "0 ~ 2，控制创意程度，默认 0.8" },
  // 联网搜索
  { group: "search", name: "bocha_api_key", label: "博查 API Key", type: "password",
    hint: "researcher 子 Agent 联网检索热点 / 痛点 / 事实用",
    link: { url: "https://open.bochaai.com/", text: "点击获取" } },
  // 视频素材
  { group: "media", name: "video_pexels_api_keys", label: "Pexels API Keys", type: "password",
    hint: "短视频素材源；多个 key 用逗号分隔轮询", recommended: true,
    link: { url: "https://www.pexels.com/api/", text: "点击获取" } },
  { group: "media", name: "video_pixabay_api_keys", label: "Pixabay API Keys", type: "password",
    hint: "可选备用素材源（Pexels 限流时切换用）",
    link: { url: "https://pixabay.com/api/docs/", text: "点击获取" } },
];

const GROUPS = [
  { id: "llm" as const,    title: "🧠 LLM 模型",     desc: "对话主 Agent + 创意期 supervisor / 编委会都靠这套" },
  { id: "search" as const, title: "🔍 联网搜索",     desc: "researcher 子 Agent 检索资料的密钥" },
  { id: "media" as const,  title: "🎬 视频素材",     desc: "短视频素材下载源（至少配一个）" },
];

// ─── 组件 ────────────────────────────────────────────────────────────────

export default function SettingsPage() {
  // tab 切换：URL ?tab=skills 同步;默认 general。
  const [tab, setTab] = useState<"general" | "skills">("general");
  useEffect(() => {
    // 客户端读 URL params,避免 SSR/hydration mismatch
    if (typeof window === "undefined") return;
    const t = new URL(window.location.href).searchParams.get("tab");
    if (t === "skills") setTab("skills");
  }, []);
  const switchTab = (next: "general" | "skills") => {
    setTab(next);
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      if (next === "general") url.searchParams.delete("tab");
      else url.searchParams.set("tab", next);
      window.history.replaceState({}, "", url.toString());
    }
  };

  const [meta, setMeta] = useState<Record<string, FieldMeta>>({});
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [visibleKeys, setVisibleKeys] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isScrolling, setIsScrolling] = useState(false);
  const scrollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 滚动时短暂显示滚动条
  const onScroll = () => {
    setIsScrolling(true);
    if (scrollTimerRef.current) clearTimeout(scrollTimerRef.current);
    scrollTimerRef.current = setTimeout(() => setIsScrolling(false), 800);
  };

  useEffect(() => {
    return () => {
      if (scrollTimerRef.current) clearTimeout(scrollTimerRef.current);
    };
  }, []);

  // 拉取当前配置
  useEffect(() => {
    let cancelled = false;
    fetch(apiUrl("/api/settings"))
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<FieldsResponse>;
      })
      .then((data) => {
        if (cancelled) return;
        setMeta(data.fields);
        const d: Record<string, string> = {};
        for (const [name, m] of Object.entries(data.fields)) {
          d[name] = String(m.value ?? "");
        }
        setDraft(d);
      })
      .catch((e) => !cancelled && setError(`加载失败：${(e as Error).message}`))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, []);

  const onChange = (name: string, value: string) => {
    setDraft((prev) => ({ ...prev, [name]: value }));
  };

  const toggleKeyVisible = (name: string) => {
    setVisibleKeys((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const onSave = async () => {
    setSaving(true);
    setError(null);
    setSavedAt(null);
    try {
      const fields: Record<string, string | number> = {};
      for (const def of FIELD_DEFS) {
        if (def.type === "number") {
          const n = Number(draft[def.name] ?? "");
          fields[def.name] = Number.isFinite(n) ? n : 0;
        } else {
          fields[def.name] = draft[def.name] ?? "";
        }
      }

      const res = await fetch(apiUrl("/api/settings"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fields }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `HTTP ${res.status}`);
      }
      setSavedAt(Date.now());

      // 刷新 meta / draft，保持密码框收起状态
      const next = await fetch(apiUrl("/api/settings"))
        .then((r) => r.json() as Promise<FieldsResponse>)
        .catch(() => null);
      if (next) {
        setMeta(next.fields);
        setVisibleKeys(new Set());
        const d: Record<string, string> = {};
        for (const [name, m] of Object.entries(next.fields)) {
          d[name] = String(m.value ?? "");
        }
        setDraft(d);
      }
    } catch (e) {
      setError(`保存失败：${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  const hasChange = useMemo(() => {
    for (const def of FIELD_DEFS) {
      const m = meta[def.name];
      if (!m) continue;
      const initial = String(m.value ?? "");
      if ((draft[def.name] ?? "") !== initial) return true;
    }
    return false;
  }, [draft, meta]);

  const statusContent = useMemo(() => {
    if (error) {
      return (
        <span className="flex items-center gap-2 text-sm text-destructive">
          <AlertCircleIcon className="size-4 shrink-0" />
          <span className="truncate">{error}</span>
        </span>
      );
    }
    if (savedAt) {
      return (
        <span className="inline-flex items-center gap-2 text-sm text-emerald-600 dark:text-emerald-400">
          <CheckCircle2Icon className="size-4 shrink-0" />
          已保存 — 即时生效（下一次对话 / 工具调用即用新值）
        </span>
      );
    }
    if (hasChange) {
      return (
        <span className="text-sm text-muted-foreground">有未保存的修改</span>
      );
    }
    return (
      <span className="text-sm text-muted-foreground">当前配置已是最新</span>
    );
  }, [error, savedAt, hasChange]);

  return (
    <div
      className={`h-dvh bg-background settings-scroll ${
        isScrolling ? "is-scrolling" : ""
      }`}
      onScroll={onScroll}
    >
      <div className="container mx-auto max-w-3xl px-6 py-8">
        {/* 顶栏 */}
        <div className="mb-6 flex items-center gap-3">
          <Link
            href="/"
            className="inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-background px-2.5 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <ArrowLeftIcon className="size-4" />
            返回对话
          </Link>
          <h1 className="flex-1 text-2xl font-bold">
            {tab === "skills" ? "Skills 管理" : "全局配置"}
          </h1>
        </div>

        {/* Tab 切换 */}
        <div className="mb-6 flex gap-2 border-b border-border/60">
          <button
            onClick={() => switchTab("general")}
            className={`inline-flex items-center gap-2 border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
              tab === "general"
                ? "border-foreground text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <SettingsIcon className="size-4" />
            通用配置
          </button>
          <button
            onClick={() => switchTab("skills")}
            className={`inline-flex items-center gap-2 border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
              tab === "skills"
                ? "border-foreground text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <PackageIcon className="size-4" />
            Skills 管理
          </button>
        </div>

        {tab === "skills" ? (
          <SkillsTab />
        ) : (
          <>
            {/* 提示条 */}
            <div className="mb-6 flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-sm text-amber-700 dark:text-amber-400">
              <AlertCircleIcon className="size-4 shrink-0 mt-0.5" />
              <div>
                <div className="font-medium">保存后即时生效，无需重启</div>
                <div className="mt-0.5 text-xs text-amber-700/80 dark:text-amber-400/80">
                  API Key 与模型配置保存后即写入 config.toml 并刷新内存；下一次对话或工具调用自动使用新值。
                </div>
              </div>
            </div>

        {/* 加载中 */}
        {loading && (
          <div className="flex items-center justify-center gap-2 py-12 text-muted-foreground">
            <Loader2Icon className="size-4 animate-spin" />
            加载当前配置中…
          </div>
        )}

        {/* 表单 */}
        {!loading && GROUPS.map((g) => (
          <section key={g.id} className="mb-8">
            <h2 className="mb-1 text-lg font-semibold">{g.title}</h2>
            <p className="mb-4 text-xs text-muted-foreground">{g.desc}</p>
            <div className="space-y-4 rounded-lg border border-border/60 bg-muted/20 p-4">
              {FIELD_DEFS.filter((f) => f.group === g.id).map((f) => {
                const m = meta[f.name];
                const isKey = m?.is_key === true;
                const isVisible = visibleKeys.has(f.name);
                const inputType =
                  f.type === "password" && !isVisible ? "password" : "text";
                const placeholder =
                  f.type === "password" ? "未配置，请输入" : "";
                return (
                  <div key={f.name}>
                    <label
                      htmlFor={f.name}
                      className="mb-1 flex items-center gap-1.5 text-sm font-medium text-foreground/90"
                    >
                      {f.label}
                      {f.recommended && (
                        <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] text-emerald-600 dark:text-emerald-400">
                          推荐
                        </span>
                      )}
                      {isKey && (m.value ?? "") !== "" && (
                        <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] text-emerald-600 dark:text-emerald-400">
                          已配置
                        </span>
                      )}
                      {f.link && (
                        <a
                          href={f.link.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="ml-auto text-xs font-normal text-sky-600 transition-colors hover:text-sky-500 hover:underline dark:text-sky-400"
                        >
                          {f.link.text}
                        </a>
                      )}
                    </label>
                    <div className="relative">
                      <input
                        id={f.name}
                        type={inputType}
                        step={f.type === "number" ? "0.1" : undefined}
                        value={draft[f.name] ?? ""}
                        onChange={(e) => onChange(f.name, e.target.value)}
                        placeholder={placeholder}
                        autoComplete={
                          f.type === "password" ? "new-password" : "off"
                        }
                        className="w-full rounded-md border border-border/60 bg-background px-3 py-2 pr-9 text-sm transition-colors focus:border-foreground/30 focus:outline-none focus:ring-2 focus:ring-foreground/10"
                      />
                      {f.type === "password" && (
                        <button
                          type="button"
                          onClick={() => toggleKeyVisible(f.name)}
                          className="absolute right-2 top-1/2 -translate-y-1/2 inline-flex size-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                          aria-label={isVisible ? "隐藏密钥" : "显示密钥"}
                        >
                          {isVisible ? (
                            <EyeOffIcon className="size-4" />
                          ) : (
                            <EyeIcon className="size-4" />
                          )}
                        </button>
                      )}
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground/80">{f.hint}</p>
                  </div>
                );
              })}
            </div>
          </section>
        ))}

        {/* 保存栏 */}
        {!loading && (
          <div className="sticky bottom-4 mt-8 flex items-center justify-between gap-3 rounded-lg border border-border/60 bg-background/95 p-3 backdrop-blur">
            <div className="min-w-0 flex-1 text-sm">{statusContent}</div>
            <Button onClick={onSave} disabled={saving || !hasChange}>
              {saving ? (
                <>
                  <Loader2Icon className="size-4 animate-spin" />
                  保存中…
                </>
              ) : (
                "保存"
              )}
            </Button>
          </div>
        )}
          </>
        )}
      </div>
    </div>
  );
}
