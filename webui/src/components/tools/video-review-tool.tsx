"use client";

import { useState } from "react";
import {
  useAssistantToolUI,
  type ToolCallMessagePartComponent,
} from "@assistant-ui/react";

/** review_video_plan 工具参数（后端 interrupt value） */
interface ReviewArgs {
  script_text?: string;
  storyboard?: Array<{ text: string; search_prompt: string }>;
}
/** 评审卡提交的改稿（作为 tool result 回传，触发 adapter.onAddToolResult） */
type ReviewResult = {
  script_text: string;
  storyboard: Array<{ text: string; search_prompt: string }>;
};

const VideoReviewRender: ToolCallMessagePartComponent<
  ReviewArgs,
  ReviewResult
> = ({ args, result, addResult }) => {
  const a = (args as ReviewArgs | null) ?? {};
  const [script, setScript] = useState(a.script_text ?? "");
  const [shots, setShots] = useState(
    (a.storyboard ?? []).map((s) => ({ text: s.text, search_prompt: s.search_prompt })),
  );

  // result 存在 = 用户已提交 → 卡片翻完成态（由 adapter.onAddToolResult 把 result 贴回 part）
  if (result) {
    return (
      <div className="my-1 rounded-lg border border-emerald-500/40 bg-emerald-500/5 px-3 py-2 text-sm text-emerald-600">
        ✅ 已确认文案与分镜，正在渲染视频…
      </div>
    );
  }

  return (
    <div className="my-2 w-full rounded-xl border border-amber-500/50 bg-amber-500/5 p-3">
      <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-amber-600">
        ⚠️ 请确认文案与分镜后下发渲染
      </div>

      <label className="mb-1 block text-xs text-muted-foreground">口播文案</label>
      <textarea
        className="mb-3 w-full resize-y rounded-md border border-border/60 bg-background px-2 py-1.5 text-sm"
        rows={5}
        value={script}
        onChange={(e) => setScript(e.target.value)}
      />

      <label className="mb-1 block text-xs text-muted-foreground">
        分镜（{shots.length}）
      </label>
      <div className="space-y-2">
        {shots.map((s, i) => (
          <div
            key={i}
            className="rounded-md border border-border/40 bg-muted/30 p-2 text-xs"
          >
            <input
              className="mb-1 w-full rounded border border-border/40 bg-background px-1.5 py-1"
              value={s.text}
              onChange={(e) =>
                setShots((prev) =>
                  prev.map((p, j) => (j === i ? { ...p, text: e.target.value } : p)),
                )
              }
            />
            <input
              className="w-full rounded border border-border/40 bg-background px-1.5 py-1 font-mono text-muted-foreground"
              value={s.search_prompt}
              onChange={(e) =>
                setShots((prev) =>
                  prev.map((p, j) =>
                    j === i ? { ...p, search_prompt: e.target.value } : p,
                  ),
                )
              }
            />
          </div>
        ))}
      </div>

      <div className="mt-3 flex justify-end">
        <button
          onClick={() => addResult({ script_text: script, storyboard: shots })}
          className="rounded-md bg-amber-500 px-4 py-1.5 text-sm font-semibold text-black transition-colors hover:bg-amber-400"
        >
          确认无误，下发渲染
        </button>
      </div>
    </div>
  );
};

/**
 * 注册 review_video_plan（HITL 评审卡）的渲染器。
 * 用户点确认 → addResult(...) → adapter.onAddToolResult 贴回 result 并唤醒 LangGraph。
 * 必须渲染在 AssistantRuntimeProvider 内部。输出 null，仅注册。
 */
export const VideoReviewToolUI = () => {
  useAssistantToolUI({
    toolName: "review_video_plan",
    render: VideoReviewRender,
    display: "standalone",
  });
  return null;
};
