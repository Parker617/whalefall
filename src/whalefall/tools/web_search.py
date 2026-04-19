"""
WebSearchTool：网络搜索内建工具。

支持后端（按优先级检测）：
1. SearXNG      — 默认使用本地/私有实例（免费，推荐）
2. DuckDuckGo   — 无需 API Key（需安装 duckduckgo-search 包，免费）
3. Tavily API   — 设置 TAVILY_API_KEY 环境变量启用（可选）

返回格式：带编号的 title / url / snippet 列表，便于 LLM 引用。
"""
from __future__ import annotations

import os
import warnings
from typing import Any, Dict, List
from urllib.parse import urljoin

from whalefall.tools.base import BuiltinTool, ToolContext

MAX_RESULTS = 8
MAX_SNIPPET_CHARS = 500
DEFAULT_SEARXNG_URL = "http://localhost:8080"
SEARXNG_TIMEOUT_SEC = 10


def _normalize_searxng_url(raw: str) -> str:
    """
    兼容多种写法：
    - "http://localhost:8080"
    - "localhost:8080"
    - "8080"
    - ":8080"
    """
    text = (raw or "").strip()
    if not text:
        return DEFAULT_SEARXNG_URL
    if text.isdigit():
        return f"http://localhost:{text}"
    if text.startswith(":") and text[1:].isdigit():
        return f"http://localhost{text}"
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return f"http://{text}"


def _format_results(query: str, results: List[Dict], backend: str) -> str:
    lines = [f"搜索「{query}」（{backend}）找到 {len(results)} 条结果：\n"]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or r.get("href") or "").strip()
        snippet = (r.get("content") or r.get("body") or r.get("snippet") or "").strip()
        if len(snippet) > MAX_SNIPPET_CHARS:
            snippet = snippet[:MAX_SNIPPET_CHARS] + "..."
        lines.append(f"[{i}] {title}")
        if url:
            lines.append(f"    URL: {url}")
        if snippet:
            lines.append(f"    {snippet}")
        lines.append("")
    return "\n".join(lines).rstrip()


class WebSearchTool(BuiltinTool):
    """执行网络搜索，返回结构化结果（title + url + snippet）。"""

    name = "web_search"
    description = (
        "执行网络搜索，返回相关网页的标题、URL 和摘要。"
        "适用于获取最新信息、查询文档、搜索资料。"
        "默认优先使用 SearXNG（可用 SEARXNG_URL 配置）。"
        "参数 query 为搜索词，num_results 可选（默认 5，最多 8）。"
    )
    read_only = True
    max_result_chars = 20_000
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索查询词",
            },
            "num_results": {
                "type": "integer",
                "description": "返回结果数量（默认 5，最多 8）",
                "default": 5,
            },
            "searxng_url": {
                "type": "string",
                "description": "可选：覆盖默认 SearXNG 地址（例如 http://localhost:8080）",
            },
        },
        "required": ["query"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        query = str(args.get("query", "")).strip()
        num = min(int(args.get("num_results") or 5), MAX_RESULTS)

        if not query:
            return "错误：query 参数不能为空"

        # 1) 优先 SearXNG（默认本地/私有实例）
        searxng_url = str(args.get("searxng_url", "")).strip() or os.environ.get("SEARXNG_URL", "").strip()
        searxng_url = _normalize_searxng_url(searxng_url)
        try:
            return self._search_searxng(query, num, searxng_url)
        except Exception:
            pass  # 降级到 DuckDuckGo / Tavily

        # 2) 降级：DuckDuckGo（免费，无需 API Key）
        try:
            return self._search_duckduckgo(query, num)
        except ImportError:
            pass
        except Exception:
            pass

        # 3) 可选：Tavily（配置了 key 才启用）
        tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
        if tavily_key:
            try:
                return self._search_tavily(query, num, tavily_key)
            except Exception as e:
                return f"搜索失败: {type(e).__name__}: {e}"

        return (
            "搜索失败：未找到可用后端。\n"
            "建议顺序：\n"
            "1) 启动 SearXNG（默认读取 http://localhost:8080 或环境变量 SEARXNG_URL）\n"
            "2) 安装 DuckDuckGo 后端：pip install duckduckgo-search\n"
            "3) 可选 Tavily：pip install tavily-python 并设置 TAVILY_API_KEY"
        )

    @staticmethod
    def _search_searxng(query: str, num: int, base_url: str) -> str:
        import requests

        base = (base_url or "").strip().rstrip("/")
        if not (base.startswith("http://") or base.startswith("https://")):
            raise ValueError(f"SearXNG URL 非法: {base_url}")

        endpoint = urljoin(base + "/", "search")
        resp = requests.get(
            endpoint,
            params={
                "q": query,
                "format": "json",
                "language": "zh-CN",
            },
            timeout=SEARXNG_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        data = resp.json() or {}
        raw = data.get("results") or []
        results = []
        for r in raw[:num]:
            results.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", ""),
                }
            )
        if not results:
            raise RuntimeError("SearXNG 未返回可用结果")
        return _format_results(query, results, backend=f"SearXNG({base})")

    @staticmethod
    def _search_tavily(query: str, num: int, api_key: str) -> str:
        from tavily import TavilyClient  # type: ignore
        client = TavilyClient(api_key=api_key)
        resp = client.search(query=query, max_results=num, search_depth="basic")
        results: List[Dict] = resp.get("results") or []
        if not results:
            return f"搜索「{query}」未返回结果。"
        return _format_results(query, results, backend="Tavily")

    @staticmethod
    def _search_duckduckgo(query: str, num: int) -> str:
        raw = None
        # 新包：ddgs（推荐）
        try:
            from ddgs import DDGS  # type: ignore
            with DDGS() as ddgs:
                raw = list(ddgs.text(query, max_results=num))
        except Exception:
            raw = None

        # 兼容旧包：duckduckgo_search
        if raw is None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                from duckduckgo_search import DDGS  # type: ignore
                with DDGS() as ddgs:
                    raw = list(ddgs.text(query, max_results=num))
        if not raw:
            return f"搜索「{query}」未返回结果。"
        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "content": r.get("body", ""),
            }
            for r in raw
        ]
        return _format_results(query, results, backend="DuckDuckGo")

    def prompt(self) -> str:
        return (
            "网络搜索（web_search）：\n"
            "- 需要最新信息、外部文档或不确定的事实时调用。\n"
            "- 优先使用本地 SearXNG（SEARXNG_URL，默认 http://localhost:8080）。\n"
            "- 引用搜索结果时附上来源 URL，不要伪造内容。\n"
            "- 已知答案或纯代码任务无需调用。"
        )
