/**
 * External Store 适配器（后端 LangGraph Checkpointer 为唯一真实源）
 *
 * 导出 useExternalChatStore() —— 返回 assistant-ui 的 ExternalStoreAdapter<BackendMessage>。
 * 前端不再维护权威历史：
 * - 切换会话时从 GET /api/history/{id} 拉取消息灌入；
 * - 发送时 onNew 流式镜像实时响应，结束后再对账一次 /api/history 保证与后端一致。
 *
 * HITL（Phase C）：视频流程在 request_review 挂起时，后端流末发 review_video_plan tool_call；
 * 前端评审卡（video-review-tool.tsx）确认后调用 resumeVideoReview() → POST /api/chat/resume，
 * 复用同一套 SSE 流式管线把渲染/总结实时回传。
 *
 * ⚠️ 关键不变量：assistant-ui 的消息转换器用 WeakMap<消息对象引用> 做缓存，
 * 原地修改消息对象不会刷新 UI。故每次 flush 必须新建 assistant 消息对象
 * 并用 setMessages(prev => prev.map(...)) 产生新数组。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type {
  AppendMessage,
  ExternalStoreAdapter,
  ExternalStoreThreadData,
  ThreadMessageLike,
} from "@assistant-ui/react";

/** 后端 API 地址（可通过环境变量配置） */
const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** Token 用量数据结构 */
export interface TokenUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  input_tokens?: number;
  output_tokens?: number;
}

let onUsageCallback: ((usage: TokenUsage) => void) | null = null;

export function onTokenUsage(cb: (usage: TokenUsage) => void) {
  onUsageCallback = cb;
}

/**
 * 补做失败轮的触发桥（模块级 ref）。
 * 失败卡片组件（video-render-tool.tsx）拿不到 adapter 内的 thread_id，
 * 通过此 ref 调用 adapter 注册的 redoFailedVideos（仿 onTokenUsage 模式）。
 * adapter 每次 render 重新赋值，保证闭包拿到最新 threadIdRef。
 */
export const redoFailedRef: { current: (() => void) | null } = { current: null };

/**
 * 运行模式（⚡全自动 / 专家审核）的模块级共享 state。
 * ComposerAction（thread.tsx）的选择器写入此 ref；onNew 发送时读取，带上 auto_review。
 * true=专家审核 / false=⚡全自动（默认，跳过审稿）。
 */
export const autoModeRef: { current: boolean } = { current: false };

/** 把后端相对路径拼成完整 URL（视频/文件服务） */
export function apiUrl(path: string): string {
  return API_BASE_URL + path;
}

/** JSON 值（递归），用于 tool-call args，与 assistant-ui ReadonlyJSONObject 兼容 */
type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

/** 后端消息内容片段（形状与 assistant-ui 的 ThreadMessageLike part 对齐） */
export type BackendPart =
  | { type: "text"; text: string }
  | {
      type: "tool-call";
      toolCallId: string;
      toolName: string;
      args?: Record<string, JsonValue>;
      argsText?: string;
      result?: unknown; // 原样 ToolMessage.content，与实时流一致
      isError?: boolean;
    };

/** 后端统一消息类型（外部存储的 T） */
export interface BackendMessage {
  id: string;
  role: "user" | "assistant";
  content: BackendPart[];
  createdAt?: string;
}

/** 后端 Part 已匹配 ThreadMessageLike，转换近恒等 */
function convertMessage(m: BackendMessage): ThreadMessageLike {
  return { id: m.id, role: m.role, content: m.content };
}

/** 从 AppendMessage 抽取纯文本 */
function extractUserText(message: AppendMessage): string {
  const content = message.content as readonly { type: string; text?: string }[];
  return content
    .filter((p) => p.type === "text")
    .map((p) => p.text ?? "")
    .join("");
}

function newId(): string {
  return crypto.randomUUID();
}

/** 列出后端会话，映射为 ExternalStoreThreadData（分页） */
async function fetchThreadsList(
  limit: number,
  offset: number,
): Promise<ExternalStoreThreadData<"regular">[]> {
  const url = new URL(`${API_BASE_URL}/api/threads`);
  url.searchParams.set("limit", String(limit));
  url.searchParams.set("offset", String(offset));
  const res = await fetch(url);
  const arr = (await res.json()) as Array<{
    id: string;
    title?: string;
  }>;
  return arr.map((t) => ({
    status: "regular" as const,
    id: t.id,
    title: t.title || "新对话",
  }));
}

/** 会话列表分页大小 */
const THREADS_PAGE_SIZE = 20;

/** 分页加载状态（暴露给 ThreadList 用） */
export interface ThreadsPagination {
  hasMore: boolean;
  isLoadingMore: boolean;
  loadMore: () => Promise<void>;
}

/** 拉取某会话的权威消息（从 Checkpointer） */
async function fetchHistory(
  threadId: string,
): Promise<{ messages: BackendMessage[]; can_continue: boolean }> {
  const res = await fetch(`${API_BASE_URL}/api/history/${threadId}`);
  if (!res.ok) return { messages: [], can_continue: false };
  const data = (await res.json()) as {
    messages?: BackendMessage[];
    can_continue?: boolean;
  };
  return { messages: data.messages ?? [], can_continue: !!data.can_continue };
}

/**
 * 外部存储 hook：持有 threads / threadId / messages / isRunning 等状态，
 * 返回 { adapter, pagination } —— adapter 喂给 useExternalStoreRuntime，
 * pagination 喂给 ThreadList 实现分页加载。
 */
export function useExternalChatStore(): {
  adapter: ExternalStoreAdapter<BackendMessage>;
  pagination: ThreadsPagination;
} {
  const [threads, setThreads] = useState<ExternalStoreThreadData<"regular">[]>(
    [],
  );
  // threadId 与浏览器 URL ?t=<id> 双向同步：lazy init 时直接从 URL 读取,
  // 后续每次变化由下方 effect 用 history.replaceState 写回 —— 刷新即恢复会话,
  // 也方便 DevTools / 分享链接直接看到 session id。
  const [threadId, setThreadId] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    return new URLSearchParams(window.location.search).get("t");
  });
  const [messages, setMessages] = useState<BackendMessage[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [isLoadingThreads, setIsLoadingThreads] = useState(true);
  const [isLoadingMessages, setIsLoadingMessages] = useState(false);
  const [hasMoreThreads, setHasMoreThreads] = useState(true);
  const [isLoadingMoreThreads, setIsLoadingMoreThreads] = useState(false);

  // refs：逃逸异步闭包，避免陈旧读取
  const abortRef = useRef<AbortController | null>(null);
  const assistantMsgRef = useRef<BackendMessage | null>(null);
  // 同时把初始 URL 值灌进 ref —— 首次渲染时不必等 effect 同步,初始挂载 effect 即可读到
  const threadIdRef = useRef<string | null>(threadId);
  // continueVideo 在 hook 内声明顺序较后,初始挂载 effect 通过 ref 转发调用
  const continueVideoRef = useRef<((id: string) => Promise<void>) | null>(null);
  const threadsRef = useRef(threads);
  // messagesRef 同步影子:streamAssistantReply(reuseLastAssistant) 需要【同步】读 messages
  // 末尾的 assistant 消息,不能用 setMessages(updater) —— 那是 React 异步更新,updater 函数
  // 在下次 render 才执行,变量赋值滞后,if 检查时拿到的是 null。用 ref 才能跨 setMessages 调用
  // 立刻读到最新值。
  const messagesRef = useRef<BackendMessage[]>(messages);
  const threadsOffsetRef = useRef(0); // 下一页起点；prepend/delete 不修，靠 ID 去重兜底
  // 同步锁：state 更新是异步的，挡不住同一渲染内/IntersectionObserver 连击的并发触发
  const isLoadingMoreRef = useRef(false);
  const hasMoreRef = useRef(true);
  useEffect(() => {
    threadIdRef.current = threadId;
    // 同步到浏览器 URL：用 replaceState 不堆栈,后退键不会在会话间反弹
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      if (threadId) {
        url.searchParams.set("t", threadId);
      } else {
        url.searchParams.delete("t");
      }
      window.history.replaceState(null, "", url.toString());
    }
  }, [threadId]);
  useEffect(() => {
    threadsRef.current = threads;
  }, [threads]);
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);
  useEffect(() => {
    hasMoreRef.current = hasMoreThreads;
  }, [hasMoreThreads]);

  // 初始挂载：拉首页 + 若 URL 提供 ?t=<id> 则并行加载该会话历史
  useEffect(() => {
    let cancelled = false;
    fetchThreadsList(THREADS_PAGE_SIZE, 0)
      .then((list) => {
        if (cancelled) return;
        setThreads(list);
        threadsOffsetRef.current = list.length;
        const stillHas = list.length >= THREADS_PAGE_SIZE;
        hasMoreRef.current = stillHas;
        setHasMoreThreads(stillHas);
        setIsLoadingThreads(false);
      })
      .catch(() => {
        if (!cancelled) setIsLoadingThreads(false);
      });
    // 若 URL 提供了 thread_id（刷新/分享链接进来）→ 立即加载其消息
    const initialId = threadIdRef.current;
    if (initialId) {
      setIsLoadingMessages(true);
      setMessages([]);
      fetchHistory(initialId)
        .then(({ messages: hydrated, can_continue }) => {
          if (cancelled || threadIdRef.current !== initialId) return;
          setMessages(hydrated);
          messagesRef.current = hydrated;  // 同步 ref —— continueVideo 紧接其后立即读取
          if (can_continue) continueVideoRef.current?.(initialId);
        })
        .catch(() => {})
        .finally(() => {
          if (!cancelled && threadIdRef.current === initialId) {
            setIsLoadingMessages(false);
          }
        });
    }
    return () => {
      cancelled = true;
    };
  }, []);

  /** 中断进行中的流 */
  function abortInFlight() {
    abortRef.current?.abort();
    abortRef.current = null;
    assistantMsgRef.current = null;
  }

  /** 把指定 assistant 消息标记为失败 */
  function failAssistant(msgId: string, text: string) {
    setIsRunning(false);
    assistantMsgRef.current = null;
    setMessages((prev) =>
      prev.map((m) =>
        m.id === msgId ? { ...m, content: [{ type: "text", text }] } : m,
      ),
    );
  }

  /**
   * 流式跑一轮 assistant 回复（onNew / resumeReview / continueVideo / redoFailed 共用）。
   * 默认追加新 assistant 占位 → POST url → SSE 循环命令式更新 → finally 收尾。
   *
   * opts.reuseLastAssistant=true 时复用末尾已渲染的 assistant message（不新建）：
   * 用于 /continue 续传 —— history reload 已经从 checkpoint 重建出 N 张批量卡到末尾
   * assistant bubble，新事件应按 toolCallId 命中那张 bubble 里的现有卡更新（编委会
   * 区块、stage 推进、render_done 翻播放器），而不是另起一个 bubble 让用户看到双重渲染。
   */
  async function streamAssistantReply(
    activeId: string,
    url: string,
    body: Record<string, unknown>,
    opts?: { reuseLastAssistant?: boolean },
  ): Promise<void> {
    let assistantMsg: BackendMessage;
    if (opts?.reuseLastAssistant) {
      // ⚠️ 不能用 setMessages(updater) 同步读 messages —— React 异步执行 updater,
      // 函数体内的赋值在下次 render 才生效,这里 if 检查时拿到的是 null,导致始终走
      // defensive 新建分支 → 双 bubble bug。改用 messagesRef 同步读取。
      let found: BackendMessage | null = null;
      const cur = messagesRef.current;
      for (let i = cur.length - 1; i >= 0; i--) {
        if (cur[i].role === "assistant") {
          found = cur[i];
          break;
        }
      }
      if (found) {
        assistantMsg = found;
      } else {
        // 防御：极端场景 history reload 没产生 assistant（DB 空 + checkpoint 空）→ 退回新建
        assistantMsg = { id: newId(), role: "assistant", content: [] };
        setMessages((prev) => [...prev, assistantMsg]);
      }
    } else {
      assistantMsg = { id: newId(), role: "assistant", content: [] };
      setMessages((prev) => [...prev, assistantMsg]);
    }
    assistantMsgRef.current = assistantMsg;
    setIsRunning(true);

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let response: Response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
    } catch (e) {
      failAssistant(assistantMsg.id, `请求失败：${(e as Error).message}`);
      return;
    }
    if (!response.ok || !response.body) {
      failAssistant(assistantMsg.id, `后端错误：${response.status}`);
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    // 有序 parts 队列：text 与 tool-call 按到达时间穿插（而非 text 全在前）。
    // 批量任务 agent 有两段文本（开头说明 + 结尾总结）,中间隔着工具卡,穿插排列
    // 让结尾总结显示在工具卡之后,而不是被拼到最前。
    const parts: BackendPart[] = [];
    const toolIndex = new Map<string, number>(); // toolCallId → 在 parts 中的下标
    let pendingText = "";

    // 复用 bubble 时:把已渲染的 parts 按原顺序 hydrate 进 parts/toolIndex/pendingText,
    // 让后续 SSE tool_call(同 toolCallId)能命中现有卡片原位更新而非追加新卡。
    if (opts?.reuseLastAssistant) {
      for (const part of assistantMsg.content) {
        if (part.type === "tool-call") {
          toolIndex.set(part.toolCallId, parts.length);
          parts.push(part);
        } else if (part.type === "text") {
          parts.push(part);
        }
      }
    }

    const flushPendingText = () => {
      if (pendingText) {
        parts.push({ type: "text", text: pendingText });
        pendingText = "";
      }
    };

    const buildContent = (): BackendPart[] => {
      // 末尾未封的 text 一并 flush（不修改 parts 原状态,产新数组）
      const out = [...parts];
      if (pendingText) out.push({ type: "text", text: pendingText });
      return out;
    };

    // flush：新建对象 + 新数组（WeakMap 缓存要求）
    const flush = () => {
      const cur = assistantMsgRef.current;
      if (!cur) return;
      const next: BackendMessage = { ...cur, content: buildContent() };
      assistantMsgRef.current = next;
      setMessages((prev) => prev.map((m) => (m.id === cur.id ? next : m)));
    };

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const dataStr = line.slice(6).trim();
          if (dataStr === "" || dataStr === "{}") continue;

          let parsed: Record<string, unknown>;
          try {
            parsed = JSON.parse(dataStr) as Record<string, unknown>;
          } catch {
            continue;
          }

          if (typeof parsed.delta === "string") {
            pendingText += parsed.delta;
            flush();
            continue;
          }
          if (parsed.tool_call && typeof parsed.tool_call === "object") {
            const tc = parsed.tool_call as {
              id: string;
              name: string;
              args?: unknown;
            };
            const args = (tc.args ?? {}) as Record<string, JsonValue>;
            // 先封当前 text 段入队,保证 text 在此 tool-call 之前
            flushPendingText();
            const idx = toolIndex.get(tc.id);
            if (idx === undefined) {
              // 新 tool-call：追加 + 记位置
              toolIndex.set(tc.id, parts.length);
              parts.push({
                type: "tool-call",
                toolCallId: tc.id,
                toolName: tc.name,
                args,
                argsText: JSON.stringify(args),
              });
            } else {
              // 同 id 后续事件：原位更新 args/argsText/toolName（progress 推进）,不挪位
              const existing = parts[idx] as BackendPart & { type: "tool-call" };
              existing.toolName = tc.name || existing.toolName;
              existing.args = args;
              existing.argsText = JSON.stringify(args);
            }
            flush();
            continue;
          }
          if (parsed.tool_result && typeof parsed.tool_result === "object") {
            const tr = parsed.tool_result as { id: string; result?: unknown };
            const idx = toolIndex.get(tr.id);
            if (idx !== undefined) {
              // 原位更新 result,不挪位
              parts[idx] = { ...(parts[idx] as BackendPart & { type: "tool-call" }), result: tr.result };
            } else {
              // 没见过 start 但来了 result —— 补一个空 tool-call 兜底
              toolIndex.set(tr.id, parts.length);
              parts.push({
                type: "tool-call",
                toolCallId: tr.id,
                toolName: "",
                args: {},
                result: tr.result,
              });
            }
            flush();
            continue;
          }
          if (parsed.usage) {
            onUsageCallback?.(parsed.usage as TokenUsage);
          }
        }
      }
    } catch (e) {
      if (!ctrl.signal.aborted) {
        pendingText += `\n\n[流式中断: ${(e as Error).message}]`;
        flush();
      }
    } finally {
      flush();
      setIsRunning(false);
      abortRef.current = null;
      assistantMsgRef.current = null;
    }
  }

  /** 发送消息：创建会话（若需）→ 流式镜像 → 对账真实源 → 首轮标题 */
  async function onNew(message: AppendMessage): Promise<void> {
    const userText = extractUserText(message);

    // 1. thread_id 由 state 持有，为空则建会话（懒创建，无空会话）
    let activeId = threadIdRef.current;
    if (!activeId) {
      const localId = newId().replace(/-/g, "");  // 完整 32 位 hex（与后端 uuid4().hex 一致，SaaS 抗冲突）
      try {
        const r = await fetch(`${API_BASE_URL}/api/threads`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ localId }),
        });
        const t = (await r.json()) as { id: string; title?: string };
        activeId = t.id;
      } catch {
        activeId = localId;
      }
      setThreadId(activeId);
      threadIdRef.current = activeId;
      setThreads((prev) => [
        { status: "regular", id: activeId!, title: "新对话" },
        ...prev,
      ]);
      // 后端按 updated_at desc 排序，新建插入第 1 位 → 下一页起点要后退 1（否则会与已显示的最后一条重叠）
      threadsOffsetRef.current += 1;
    }

    // 2. 乐观追加 user 消息
    setMessages((prev) => [
      ...prev,
      { id: newId(), role: "user", content: [{ type: "text", text: userText }] },
    ]);

    // 3. 流式 assistant 回复（含可能的 review_video_plan tool_call）。
    //    不再对账：用 checkpoint id 覆盖随机 id 会触发 assistant-ui 分支，且 SSE 已是完整输出。
    //    auto_review：用户当前选择的运行模式（⚡全自动=跳过审稿）。
    await streamAssistantReply(activeId, `${API_BASE_URL}/api/chat/send`, {
      message: userText,
      thread_id: activeId,
      auto_review: autoModeRef.current,
    });

    // 5. 首轮自动标题
    const current = threadsRef.current.find((t) => t.id === activeId);
    if (current && current.title === "新对话") {
      fetch(`${API_BASE_URL}/api/threads/${activeId}/title`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: [{ role: "user", content: [{ type: "text", text: userText }] }],
        }),
      })
        .then((r) => r.json())
        .then((d: { title?: string }) => {
          if (d.title) {
            setThreads((prev) =>
              prev.map((t) =>
                t.id === activeId ? { ...t, title: d.title! } : t,
              ),
            );
          }
        })
        .catch(() => {});
    }
  }

  /**
   * 编辑已发送的用户消息（伪编辑）。
   *
   * 后端 LangGraph checkpoint 不可变、无「编辑历史消息并从该点重跑」能力,
   * 所以这里直接复用 onNew —— 把编辑后的文本当成一条新用户消息走 SSE 流。
   * 历史保留原消息,末尾追加新的一轮,体感上像「改完重发」。
   * 不做真编辑,避免破坏 checkpoint 不可变性 + 大改后端。
   */
  async function onEdit(message: AppendMessage): Promise<void> {
    await onNew(message);
  }

  async function onSwitchToNewThread() {
    abortInFlight();
    setThreadId(null);
    threadIdRef.current = null;
    setMessages([]);
    setIsRunning(false);
  }

  async function onSwitchToThread(id: string) {
    if (threadIdRef.current === id) return;
    abortInFlight();
    setThreadId(id);
    threadIdRef.current = id;
    setIsLoadingMessages(true);
    setMessages([]); // 立即清空，避免跨会话闪烁
    try {
      const { messages: hydrated, can_continue } = await fetchHistory(id);
      if (threadIdRef.current === id) {
        setMessages(hydrated); // 守卫：防快速切换串话
        messagesRef.current = hydrated; // 同步 ref —— continueVideo 紧接其后立即读取
        // 断点续传：重启后该会话有未完成的渲染（有 pending 节点 + 已越过断点）→ 自动续跑
        if (can_continue) continueVideo(id);
      }
    } finally {
      if (threadIdRef.current === id) setIsLoadingMessages(false);
    }
  }

  /** 断点续传：从 checkpoint 续跑未完成的视频渲染（/api/chat/continue 流式追加 summary + 播放器）
   *
   * reuseLastAssistant=true：复用 history reload 已经渲染出来的末尾 assistant bubble。
   * 后端 /continue stream 里的事件按 toolCallId 命中已有卡片就地更新（stage 推进、
   * render_done 翻播放器、agent_state 编委会区块灰化）。避免新建第二个 bubble。
   */
  async function continueVideo(activeId: string) {
    try {
      await streamAssistantReply(
        activeId,
        `${API_BASE_URL}/api/chat/continue`,
        { thread_id: activeId },
        { reuseLastAssistant: true },
      );
    } catch {
      /* 409（已有活跃渲染）等忽略，下次加载见完成 */
    }
  }
  // 注册到 ref —— 初始挂载 effect（声明顺序在前）通过 ref 转发调用
  continueVideoRef.current = continueVideo;

  /** 补做失败轮：Command(goto=batch_redo_start) 复用原 task_id 只重做失败子步。
   *  进度/成片事件用原 batch_index 的 card_id（render-batch-{原位置}），
   *  在新 assistant 消息里渲染对应位置的卡片，与原失败卡视觉对应。 */
  async function redoFailedVideos() {
    const activeId = threadIdRef.current;
    if (!activeId) return;
    try {
      await streamAssistantReply(
        activeId,
        `${API_BASE_URL}/api/chat/redo-failed`,
        { thread_id: activeId },
      );
    } catch {
      /* 409（已有活跃渲染）等忽略 */
    }
  }
  redoFailedRef.current = redoFailedVideos;

  async function onRename(id: string, newTitle: string) {
    await fetch(`${API_BASE_URL}/api/threads/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: newTitle }),
    });
    setThreads((prev) =>
      prev.map((t) => (t.id === id ? { ...t, title: newTitle } : t)),
    );
  }

  async function onDelete(id: string) {
    await fetch(`${API_BASE_URL}/api/threads/${id}`, { method: "DELETE" });
    let removed = false;
    setThreads((prev) => {
      const next = prev.filter((t) => t.id !== id);
      removed = next.length !== prev.length;
      return next;
    });
    if (removed) {
      threadsOffsetRef.current = Math.max(0, threadsOffsetRef.current - 1);
    }
    if (threadIdRef.current === id) {
      abortInFlight();
      setThreadId(null);
      threadIdRef.current = null;
      setMessages([]);
      setIsRunning(false);
    }
  }

  const adapter: ExternalStoreAdapter<BackendMessage> = {
    messages,
    isRunning,
    isLoading: isLoadingMessages,
    convertMessage,
    onNew,
    // 伪编辑：后端 checkpoint 不可变、无「编辑历史消息重跑」能力,所以把编辑后的文本
    // 当成一条新用户消息直接走 onNew 流程(复用 SSE 流)。历史保留原消息,末尾追加新轮次,
    // 体感上像「改完重发」。避免点 Edit 时抛 "Runtime does not support editing" 崩溃。
    onEdit,
    onAddToolResult: async (options) => {
      if (options.toolName !== "review_video_plan") return;
      // 1. 把 result 写回 review_video_plan tool-call part（卡片翻完成态；external-store 不自动贴）
      setMessages((prev) =>
        prev.map((m) => ({
          ...m,
          content: m.content.map((p) =>
            p.type === "tool-call" && p.toolCallId === options.toolCallId
              ? { ...p, result: options.result }
              : p,
          ),
        })),
      );
      // 2. 用 result 里的改稿唤醒 LangGraph，流式追加新 assistant 消息（总结 + player）
      const activeId = threadIdRef.current;
      if (!activeId) return;
      const edited = (options.result ?? {}) as {
        script_text?: string;
        storyboard?: Array<{ text: string; search_prompt: string }>;
      };
      await streamAssistantReply(activeId, `${API_BASE_URL}/api/chat/resume`, {
        thread_id: activeId,
        script_text: edited.script_text ?? "",
        storyboard: edited.storyboard ?? [],
      });
    },
    onCancel: async () => {
      abortRef.current?.abort();
    },
    setMessages: (m) => setMessages([...m]),
    adapters: {
      threadList: {
        threadId: threadId ?? undefined,
        isLoading: isLoadingThreads,
        threads,
        onSwitchToNewThread,
        onSwitchToThread,
        onRename,
        onDelete,
      },
    },
  };

  /** 触底加载下一页（首屏非空 + 还有更多 + 不在加载中才动） */
  const loadMore = useCallback(async () => {
    // 同步锁：ref 在调用栈内立即生效，挡住 IntersectionObserver 重绑/连击触发的并发
    if (isLoadingMoreRef.current || !hasMoreRef.current) return;
    isLoadingMoreRef.current = true;
    setIsLoadingMoreThreads(true);
    try {
      const more = await fetchThreadsList(
        THREADS_PAGE_SIZE,
        threadsOffsetRef.current,
      );
      const seen = new Set(threadsRef.current.map((t) => t.id));
      const fresh = more.filter((t) => !seen.has(t.id));
      if (fresh.length) {
        setThreads((prev) => [...prev, ...fresh]);
      }
      // 后端游标按本次实际返回数推进（不满 limit 即到底）
      threadsOffsetRef.current += more.length;
      const stillHas = more.length >= THREADS_PAGE_SIZE;
      hasMoreRef.current = stillHas;
      setHasMoreThreads(stillHas);
    } catch {
      /* 失败保持当前状态，下次再试 */
    } finally {
      isLoadingMoreRef.current = false;
      setIsLoadingMoreThreads(false);
    }
  }, []); // 引用稳定 → 哨兵 useEffect 不会重绑 IntersectionObserver

  return {
    adapter,
    pagination: {
      hasMore: hasMoreThreads,
      isLoadingMore: isLoadingMoreThreads,
      loadMore,
    },
  };
}
