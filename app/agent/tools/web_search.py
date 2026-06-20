"""
Web 搜索工具

使用博查AI搜索（bochaai.com）作为搜索后端。
国内直连，专为AI应用设计，返回干净文本片段。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from langchain_core.tools import tool

from agent.config import settings

logger = logging.getLogger(__name__)

# 博查 Web Search API 地址
BOCHA_API_URL = "https://api.bocha.cn/v1/web-search"


@tool
async def web_search(query: str, max_results: int | None = None) -> list[dict[str, Any]]:
    """
    联网搜索工具。

    当需要获取最新互联网信息、时事新闻、天气、股价、赛事结果等
    超出模型训练截止日期的知识时调用。
    """
    api_key = settings.bocha_api_key
    if not api_key:
        # 未配置博查 key → 不 raise,返回空结果让 researcher 凭模型自身知识继续(降级但不崩)。
        # 若 raise ValueError 会击穿 ToolNode(默认只兜 ToolInvocationError)→ 击穿 SSE 流,
        # 前端视频卡片永久卡住。与 key 错误(401/403)走同一降级路径,行为一致。
        logger.warning("[web_search] query=%r 博查 API Key 未配置,返回空结果(联网检索降级)", query)
        return []

    count = max_results or settings.search_max_results

    request_body = {
        "query": query,
        "freshness": "noLimit",
        "summary": True,
        "count": count,
    }

    logger.info("[web_search] query=%r count=%d", query, count)

    # timeout 分离：代理场景 TLS 握手需 connect 时间，博查后端搜索需 read 时间
    timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                BOCHA_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=request_body,
            )
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.TimeoutException, httpx.HTTPError, httpx.TransportError) as e:
        # 网络抖动 / 超时 / 代理 TLS 失败 / key 失效(401/403 raise_for_status) → 不崩流,
        # 返回空结果让 researcher 用其它 query 继续(或凭模型自身知识降级)
        logger.warning("[web_search] query=%r 网络异常，返回空结果：%s: %s", query, type(e).__name__, str(e)[:120])
        return []

    web_pages = payload.get("data", {}).get("webPages", {}).get("value", [])

    results = []
    for page in web_pages:
        results.append({
            "title": page.get("name", ""),
            "url": page.get("url", ""),
            "snippet": page.get("summary") or page.get("snippet", ""),
            "siteName": page.get("siteName", ""),
            "datePublished": page.get("datePublished", ""),
        })

    logger.info("[web_search] 返回 %d 条结果", len(results))
    return results
