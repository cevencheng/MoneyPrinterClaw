"use client";

import { useState } from "react";
import {
  useAssistantToolUI,
  type ToolCallMessagePartComponent,
} from "@assistant-ui/react";
import {
  CheckIcon,
  DownloadIcon,
  LoaderIcon,
  RotateCcwIcon,
} from "lucide-react";
import { apiUrl, redoFailedRef } from "@/lib/chat-adapter";
import { cn } from "@/lib/utils";

/** 工业渲染线 5 阶段（顺序固定，前端内置；后端只下发当前 stage） */
const STAGES: Array<{ key: string; icon: string; label: string }> = [
  { key: "tts", icon: "🎙️", label: "配音合成" },
  { key: "asr", icon: "🔤", label: "字幕识别" },
  { key: "materials", icon: "🔍", label: "搜索 & 下载素材" },
  { key: "combine", icon: "🎬", label: "拼接素材片段" },
  { key: "generate", icon: "🎞️", label: "叠音轨 / 字幕 / BGM" },
];

/** 智能编委会 4 个 Agent（顺序固定，对应 supervisor 子图节点名） */
const AGENTS: Array<{ key: string; icon: string; label: string }> = [
  { key: "researcher", icon: "🔍", label: "资料研究员" },
  { key: "editor", icon: "✍️", label: "金牌主编" },
  { key: "reviewer", icon: "⚖️", label: "首席质检官" },
  { key: "director", icon: "🎬", label: "分镜导演" },
];

interface AgentNodeStatus {
  status: "active" | "done";
  meta: string;
}

interface AgentBoardState {
  active_node: string | null;
  node_history: Record<string, AgentNodeStatus>;
  log_message: string;
}

interface ProgressArgs {
  stage?: string;
  label?: string;
  /** 任务 ID（后端 video_task_id，16 位 hex；卡片标题下显示） */
  task_id?: string;
  // 批量元信息（后端进度事件透传；单视频时缺失 → 标题不显示序号）
  batch_index?: number;
  batch_total?: number;
  topic?: string;
  /** 多 Agent 编委会状态（创意期实时刷；缺失则不渲染编委会区块——向后兼容） */
  agent_state?: AgentBoardState;
}
interface RenderResult {
  url?: string;
  filename?: string;
}

/** 卡片标题：批量时 `1/2 · 咖啡`；单视频/缺失时退化为 topic 或空串。 */
function renderTitle(a: ProgressArgs): string {
  const prefix = a.batch_index && a.batch_total ? `${a.batch_index}/${a.batch_total} · ` : "";
  const topic = a.topic || "";
  return `${prefix}${topic}`.trim();
}

/** 标题下方的任务 ID 行（16 位 hex；缺失时不渲染）。 */
function TaskIdLine({ taskId }: { taskId?: string }) {
  if (!taskId) return null;
  return (
    <div className="font-mono text-[10px] leading-tight text-muted-foreground/50">
      任务ID：{taskId}
    </div>
  );
}

/** 智能编委会区块：4 个 Agent 状态机 (waiting/active/done) + meta 战果。
 * 高级智能体协同大剧组面板：高对比度 + 赛博科技胶囊 + 成果资产微缩标签。 */
function AgentBoard({ state, dim }: { state: AgentBoardState; dim?: boolean }) {
  return (
    <div
      className={cn(
        "space-y-3 rounded-xl border border-slate-200/60 bg-slate-50/80 p-4 shadow-sm dark:border-slate-800/80 dark:bg-slate-900/40",
        dim && "opacity-55",
      )}
    >
      <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
        <span className={cn("size-1.5 rounded-full bg-indigo-500", !dim && "animate-pulse")} />
        🧠 智能编委会
      </div>
      <div className="space-y-2">
        {AGENTS.map((a) => {
          const s = state.node_history[a.key];
          const status = s?.status;
          const meta = s?.meta || "";
          const isActive = status === "active";
          const isDone = status === "done";
          return (
            <div key={a.key}>
              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  {isDone ? (
                    <span className="flex size-4 shrink-0 items-center justify-center rounded-full bg-emerald-500/20 text-[10px] font-bold text-emerald-600">
                      ✓
                    </span>
                  ) : isActive ? (
                    <span className="relative flex size-4 shrink-0 items-center justify-center">
                      <span className="absolute inline-flex size-full rounded-full bg-indigo-400 opacity-75 animate-ping" />
                      <span className="relative inline-flex size-3.5 items-center justify-center rounded-full bg-indigo-500 text-[9px] text-white">
                        ⚡
                      </span>
                    </span>
                  ) : (
                    <span className="flex size-4 shrink-0 items-center justify-center text-xs opacity-50">
                      {a.icon}
                    </span>
                  )}
                  {/* 岗位名称：钉死高对比，彻底废弃浅灰字 */}
                  <span className="truncate text-sm font-semibold text-slate-800 dark:text-slate-200">
                    {a.label}
                  </span>
                </div>
                {/* 状态胶囊：active 橙色呼吸科技胶囊 / done 荧光绿胶囊+对勾 / waiting 不挂 */}
                {isActive && (
                  <span className="animate-pulse shrink-0 rounded-full border border-orange-500/30 bg-gradient-to-r from-orange-500/20 to-amber-500/10 px-2 py-0.5 text-xs font-medium text-orange-600 dark:text-orange-400">
                    运行中
                  </span>
                )}
                {isDone && (
                  <span className="flex shrink-0 items-center gap-1 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-600 dark:text-emerald-400">
                    <CheckIcon className="size-3" />
                    完成
                  </span>
                )}
              </div>
              {/* 成果资产微缩标签：done 出成果时下方挂载 */}
              {isDone && meta && (
                <span className="mt-1 ml-6 inline-block rounded border border-slate-200/40 bg-slate-100 px-2 py-1 font-mono text-xs text-slate-500 dark:bg-slate-800/60">
                  {meta}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** 底部剧组动态弹幕（log_message） */
function LiveLogger({ message, dim }: { message: string; dim?: boolean }) {
  if (!message) return null;
  return (
    <div className="mt-3 flex items-center gap-2 border-t border-border/30 pt-2 text-[11px]">
      <span className={cn("size-1.5 rounded-full bg-emerald-500", !dim && "animate-pulse")} />
      <span className="text-muted-foreground/70">[剧组动态]</span>
      <span
        className={cn(
          "min-w-0 flex-1 truncate font-medium",
          dim ? "text-muted-foreground/60" : "text-emerald-600",
        )}
      >
        {message}
      </span>
    </div>
  );
}

const VideoRenderComponent: ToolCallMessagePartComponent<
  ProgressArgs,
  RenderResult
> = ({ args, result }) => {
  const a = (args as ProgressArgs | null) ?? {};
  const title = renderTitle(a);
  const board = a.agent_state;
  const isCreative = a.stage === "creative";
  const isPipeline = STAGES.some((s) => s.key === a.stage);
  // 失败卡"重试"按钮本地防抖：点击后 disable，由后端 SSE 流在新消息里呈现补做进度/成片。
  // 后端 _active_video_threads 409 是并发权威锁（兜住多标签页/刷新重连）。
  const [redoing, setRedoing] = useState(false);

  // 完成态：编委会（灰化保留）+ 视频播放器 + 下载
  if (result && result.url) {
    const src = apiUrl(result.url);
    return (
      <div className="my-2 w-full max-w-md overflow-hidden rounded-xl border border-emerald-500/40 bg-muted/30">
        <div className="border-b border-border/40 px-3 py-1.5 text-xs text-emerald-600">
          <div className="flex items-center gap-2">
            <CheckIcon className="size-3.5" />
            {title && <span className="font-medium">{title}</span>}
            视频已生成,可预览 / 下载
          </div>
          <TaskIdLine taskId={a.task_id} />
        </div>
        {board && (
          <div className="px-3 py-3">
            <AgentBoard state={board} dim />
          </div>
        )}
        <video controls preload="metadata" className="w-full bg-black" src={src}>
          您的浏览器不支持视频播放。
        </video>
        <div className="flex justify-end px-3 py-1.5">
          <a
            href={src}
            download={result.filename || "video.mp4"}
            className="inline-flex items-center gap-1 rounded-md border border-border/60 bg-background px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <DownloadIcon className="size-3" /> 下载
          </a>
        </div>
      </div>
    );
  }

  // 排队态（批量串行：尚未轮到本任务）—— 轻量展示，无进度条
  if (a.stage === "queued") {
    return (
      <div className="my-2 w-full max-w-md rounded-xl border border-border/50 bg-muted/20 p-3 opacity-70">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <span className="text-base">⏳</span>
          <span className="font-medium text-foreground/80">{title}</span>
          <span>排队中…</span>
        </div>
        <TaskIdLine taskId={a.task_id} />
      </div>
    );
  }

  // 失败态（批量历史轮 render 未出片）—— 错误隔离后该轮标记失败，不影响其它轮。
  // 提供"重试"按钮：复用原任务只重做失败子步（后端 /api/chat/redo-failed）。
  if (a.stage === "failed") {
    return (
      <div className="my-2 w-full max-w-md rounded-xl border border-red-500/40 bg-red-500/5 p-3">
        <div className="text-sm text-red-600">
          <div className="flex items-center gap-2">
            <span className="text-base">❌</span>
            <span className="font-medium">{title}</span>
            <span>制作失败</span>
          </div>
          <TaskIdLine taskId={a.task_id} />
        </div>
        <div className="mt-2 flex justify-end">
          <button
            type="button"
            disabled={redoing}
            onClick={() => {
              setRedoing(true);
              redoFailedRef.current?.();
            }}
            className="inline-flex items-center gap-1 rounded-md border border-border/60 bg-background px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
          >
            {redoing ? (
              <>
                <LoaderIcon className="size-3 animate-spin" />
                重试中…
              </>
            ) : (
              <>
                <RotateCcwIcon className="size-3" />
                重试
              </>
            )}
          </button>
        </div>
      </div>
    );
  }

  // 进行中态（创意期 OR 渲染期）：双期双轨卡片
  // - 创意期 (stage="creative")：编委会高亮 + 渲染线灰化预览
  // - 渲染期 (stage∈STAGES)：编委会灰化保留 + 渲染线高亮
  // - 缺 agent_state（老 DB / hydration / 补做跳 creative）：回退到纯渲染线视图
  const currentIdx = STAGES.findIndex((s) => s.key === a.stage);
  const doneCount = currentIdx > 0 ? currentIdx : 0;

  return (
    <div className="my-2 w-full max-w-md rounded-xl border border-amber-500/40 bg-amber-500/5 p-3">
      <div className="mb-3 space-y-0.5">
        <div className="flex items-center gap-2 text-sm font-semibold text-amber-600">
          <LoaderIcon className="size-4 animate-spin" />
          {title || "正在为你制作视频…"}
        </div>
        <TaskIdLine taskId={a.task_id} />
      </div>

      {/* 🧠 智能编委会区块（创意期高亮，渲染期灰化保留） */}
      {board && (
        <div className="mb-3">
          <AgentBoard state={board} dim={isPipeline} />
        </div>
      )}

      {/* ⚙️ 工业渲染线区块（创意期灰化预览，渲染期高亮） */}
      <div className={cn(isCreative && "opacity-55")}>
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground/70">
          ⚙️ 工业渲染线
        </div>
        <div className="space-y-1.5">
          {STAGES.map((s, i) => {
            const done = i < currentIdx;
            const active = i === currentIdx;
            return (
              <div
                key={s.key}
                className={cn(
                  "flex items-center gap-2 text-sm transition-colors",
                  active
                    ? "text-amber-600"
                    : done
                      ? "text-emerald-600"
                      : "text-muted-foreground/40",
                )}
              >
                <span className="flex size-4 items-center justify-center">
                  {done ? (
                    <CheckIcon className="size-3.5" />
                  ) : active ? (
                    <LoaderIcon className="size-3.5 animate-spin" />
                  ) : (
                    <span className="text-xs">{s.icon}</span>
                  )}
                </span>
                <span>{active ? a.label || s.label : `${s.icon} ${s.label}`}</span>
              </div>
            );
          })}
        </div>
        <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-muted">
          <div
            className="h-full rounded-full bg-amber-500 transition-all duration-500"
            style={{ width: `${(doneCount / STAGES.length) * 100}%` }}
          />
        </div>
        <div className="mt-1 text-right text-xs text-muted-foreground">
          阶段 {Math.max(currentIdx + 1, 1)}/{STAGES.length}
        </div>
      </div>

      {/* 📜 剧组动态弹幕（log_message 实时流，supervisor 调度文案） */}
      {board && <LiveLogger message={board.log_message} dim={isPipeline} />}
    </div>
  );
};

/**
 * 注册 render_video（视频制作进度卡 → 成片播放器）的渲染器。
 * 双期双轨：
 * - 🧠 智能编委会（创意期）：researcher / editor / reviewer / director 4 Agent 实时接力，
 *   pulse 动效 + 战果 meta（已检索 N 次 / 198 字文案 / 质检通过 / 6 个分镜）
 * - ⚙️ 工业渲染线（5 阶段）：TTS / ASR / 素材 / 拼接 / 合成
 * - 📜 剧组动态：底部弹幕显示 supervisor 调度文案
 * 由后端 adispatch_custom_event（creative_started/progress/render_done）+ supervisor 子图
 * 节点 on_chain_start/end 事件驱动；同 toolCallId 的 args.agent_state 跨事件累积。
 * 批量多轮：后端按 task_id 发独立 toolCallId → 前端自动渲染多张堆叠卡片。
 */
export const VideoRenderToolUI = () => {
  useAssistantToolUI({
    toolName: "render_video",
    render: VideoRenderComponent,
    display: "standalone",
  });
  return null;
};
