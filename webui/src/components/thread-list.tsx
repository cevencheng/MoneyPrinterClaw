import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  AuiIf,
  ThreadListItemMorePrimitive,
  ThreadListItemPrimitive,
  ThreadListPrimitive,
  useAuiState,
} from "@assistant-ui/react";
import {
  ArchiveIcon,
  Loader2Icon,
  MoreHorizontalIcon,
  PlusIcon,
  TrashIcon,
} from "lucide-react";
import { Fragment, useEffect, useMemo, useRef, type FC } from "react";
import type { ThreadsPagination } from "@/lib/chat-adapter";

interface ThreadListProps {
  pagination: ThreadsPagination;
  /** 折叠态：只渲染新建按钮（图标版），不渲染列表/哨兵 */
  collapsed?: boolean;
}

export const ThreadList: FC<ThreadListProps> = ({ pagination, collapsed = false }) => {
  if (collapsed) {
    // 折叠态：仅新建图标按钮（复用 ThreadListPrimitive.New 的会话创建逻辑）
    return (
      <ThreadListPrimitive.Root className="aui-root aui-thread-list-root flex h-full min-h-0 flex-col p-1.5">
        <ThreadListNew collapsed />
      </ThreadListPrimitive.Root>
    );
  }
  return (
    <ThreadListPrimitive.Root className="aui-root aui-thread-list-root flex h-full min-h-0 flex-col">
      {/* 固定顶部：新建按钮 */}
      <div className="shrink-0 px-1.5 pt-1.5">
        <ThreadListNew />
      </div>
      {/* 滚动区：列表 + 哨兵（IntersectionObserver 触底加载下一页） */}
      <div className="aui-thread-list-scroll flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-1.5 pb-2">
        <AuiIf condition={(s) => s.threads.isLoading}>
          <ThreadListSkeleton />
        </AuiIf>
        <AuiIf condition={(s) => !s.threads.isLoading}>
          <ThreadListItems />
        </AuiIf>
        <LoadMoreSentinel
          hasMore={pagination.hasMore}
          isLoading={pagination.isLoadingMore}
          onLoadMore={pagination.loadMore}
        />
      </div>
    </ThreadListPrimitive.Root>
  );
};

const DAY_IN_MS = 86_400_000;

const dateGroupLabel = (
  date: Date | undefined,
  startOfToday: number,
): string => {
  if (!date || date.getTime() >= startOfToday) return "Today";
  if (date.getTime() >= startOfToday - DAY_IN_MS) return "Yesterday";
  return "Earlier";
};

type ThreadListGroup = { label: string; indices: number[] };

const ThreadListItems: FC = () => {
  const threadIds = useAuiState((s) => s.threads.threadIds);
  const threadItems = useAuiState((s) => s.threads.threadItems);

  const groups = useMemo<ThreadListGroup[] | null>(() => {
    const itemsById = new Map(threadItems.map((item) => [item.id, item]));
    const dates = threadIds.map((id) => itemsById.get(id)?.lastMessageAt);
    if (!dates.some(Boolean)) return null;

    const now = new Date();
    const startOfToday = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate(),
    ).getTime();
    const time = (index: number) =>
      dates[index]?.getTime() ?? Number.MAX_SAFE_INTEGER;
    const indices = threadIds
      .map((_, index) => index)
      .sort((a, b) => time(b) - time(a));

    const result: ThreadListGroup[] = [];
    for (const index of indices) {
      const label = dateGroupLabel(dates[index], startOfToday);
      const lastGroup = result[result.length - 1];
      if (lastGroup?.label === label) {
        lastGroup.indices.push(index);
      } else {
        result.push({ label, indices: [index] });
      }
    }
    return result;
  }, [threadIds, threadItems]);

  if (!groups) {
    return (
      <ThreadListPrimitive.Items>
        {() => <ThreadListItem />}
      </ThreadListPrimitive.Items>
    );
  }

  return groups.map((group) => (
    <Fragment key={group.label}>
      <div className="aui-thread-list-group-label text-muted-foreground px-2.5 pt-3 pb-1 text-xs font-medium">
        {group.label}
      </div>
      {group.indices.map((index) => (
        <ThreadListPrimitive.ItemByIndex
          key={threadIds[index]}
          index={index}
          components={{ ThreadListItem }}
        />
      ))}
    </Fragment>
  ));
};

const ThreadListNew: FC<{ collapsed?: boolean }> = ({ collapsed = false }) => {
  if (collapsed) {
    // 折叠态：仅图标按钮，tooltip 显示完整文案
    return (
      <ThreadListPrimitive.New
        render={
          <Button
            variant="ghost"
            title="新建视频任务"
            className="aui-thread-list-new hover:bg-muted h-9 w-full justify-center rounded-md p-0"
          />
        }
      >
        <PlusIcon className="size-4" />
      </ThreadListPrimitive.New>
    );
  }
  return (
    <ThreadListPrimitive.New render={<Button variant="ghost" className="aui-thread-list-new hover:bg-muted data-active:bg-muted h-11 justify-start gap-2 rounded-md px-2.5 text-sm font-normal" />}><PlusIcon className="size-4" />新建视频任务
            </ThreadListPrimitive.New>
  );
};

const ThreadListSkeleton: FC = () => {
  return (
    <div className="flex flex-col gap-0.5">
      {Array.from({ length: 5 }, (_, i) => (
        <div
          key={i}
          role="status"
          aria-label="Loading threads"
          className="aui-thread-list-skeleton-wrapper flex h-8 items-center px-2.5"
        >
          <Skeleton className="aui-thread-list-skeleton h-3.5 w-full" />
        </div>
      ))}
    </div>
  );
};

interface LoadMoreSentinelProps {
  hasMore: boolean;
  isLoading: boolean;
  onLoadMore: () => void;
}

/** 列表底部哨兵：进入视口即触发下一页加载（hasMore=false 时不渲染，避免反复触发）
 *
 * 关键点：IntersectionObserver 的 root 必须指向我们的内部滚动容器，
 * 不能用默认的 viewport——否则在「flex + overflow-y-auto 内部滚动」结构下
 * 浏览器不会为内部 scroll 重新计算 target 与 viewport 的交集，IO 不会触发。
 */
const LoadMoreSentinel: FC<LoadMoreSentinelProps> = ({
  hasMore,
  isLoading,
  onLoadMore,
}) => {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!hasMore) return;
    const el = ref.current;
    if (!el) return;
    // 找到带 aui-thread-list-scroll 标记的最近滚动祖先（thread-list.tsx Root 里设的）
    const scrollRoot = el.closest<HTMLElement>(".aui-thread-list-scroll");
    const ob = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) onLoadMore();
      },
      {
        root: scrollRoot ?? null, // 没找到就退化用 viewport
        rootMargin: "200px 0px", // 提前 200px 触发，避免必须滚到完全可见
        threshold: 0,
      },
    );
    ob.observe(el);
    return () => ob.disconnect();
  }, [hasMore, onLoadMore]);

  if (!hasMore) return null;
  return (
    <div
      ref={ref}
      className="flex h-9 shrink-0 items-center justify-center text-xs text-muted-foreground"
      aria-live="polite"
    >
      {isLoading ? (
        <span className="flex items-center gap-1.5">
          <Loader2Icon className="size-3.5 animate-spin" />
          加载中…
        </span>
      ) : (
        <span className="opacity-60">下滑加载更多</span>
      )}
    </div>
  );
};

const ThreadListItem: FC = () => {
  return (
    <ThreadListItemPrimitive.Root className="aui-thread-list-item group flex items-center gap-1 rounded-md border-l-4 border-transparent py-3 transition-colors hover:bg-muted focus-visible:bg-muted focus-visible:outline-none data-active:border-blue-500 data-active:bg-blue-500/10 data-active:font-semibold">
      <ThreadListItemPrimitive.Trigger className="aui-thread-list-item-trigger flex h-full min-w-0 flex-1 items-center px-2.5 text-start text-sm">
        <span className="aui-thread-list-item-title min-w-0 flex-1 truncate">
          <ThreadListItemPrimitive.Title fallback="New Chat" />
        </span>
      </ThreadListItemPrimitive.Trigger>
      <ThreadListItemMore />
    </ThreadListItemPrimitive.Root>
  );
};

const ThreadListItemMore: FC = () => {
  return (
    <ThreadListItemMorePrimitive.Root>
      <ThreadListItemMorePrimitive.Trigger render={<Button variant="ghost" size="icon" className="aui-thread-list-item-more data-[state=open]:bg-accent me-1.5 size-6 p-0 opacity-0 transition-opacity group-hover:opacity-100 group-data-active:opacity-100 data-[state=open]:opacity-100" />}><MoreHorizontalIcon className="size-3.5" /><span className="sr-only">More options</span></ThreadListItemMorePrimitive.Trigger>
      <ThreadListItemMorePrimitive.Content
        side="right"
        align="start"
        sideOffset={6}
        className="aui-thread-list-item-more-content bg-popover/95 text-popover-foreground data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 data-[state=open]:animate-in data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=closed]:animate-out data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 z-50 min-w-[8rem] overflow-hidden rounded-xl border p-1.5 shadow-lg backdrop-blur-sm"
      >
        <ThreadListItemPrimitive.Archive render={<ThreadListItemMorePrimitive.Item className="aui-thread-list-item-more-item hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none" />}><ArchiveIcon className="size-4" />Archive
                        </ThreadListItemPrimitive.Archive>
        <ThreadListItemPrimitive.Delete render={<ThreadListItemMorePrimitive.Item className="aui-thread-list-item-more-item text-destructive hover:bg-destructive/10 hover:text-destructive focus:bg-destructive/10 focus:text-destructive flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none" />}><TrashIcon className="size-4" />Delete
                        </ThreadListItemPrimitive.Delete>
      </ThreadListItemMorePrimitive.Content>
    </ThreadListItemMorePrimitive.Root>
  );
};
