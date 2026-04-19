"""
LLMClient：面向业务的 LLM 门面类。

对外保留原有接口：
  - call_llm()           同步单轮（ContextManager 压缩、摘要等）
  - call_llm_async()     异步单轮
  - stream_with_tools()  异步流式工具对话（AgentLoop 主循环）
  - count_tokens / truncate_* / clean_main_text / _clean_json

底层实现已拆成两组可独立复用的模块：
  - llm/gateway/   ：openai 客户端工厂、响应校验
  - llm/postprocess/：JSON 清洗、文本清洗、tiktoken 封装

本文件只负责组合与状态管理（客户端缓存、token 状态、模型名解析），
尽量不做业务逻辑。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union

from openai import AsyncOpenAI, OpenAI

from whalefall.llm.config import get_model_info
from whalefall.llm.gateway import (
    client_cache_key,
    completion_first_message,
    make_async_client,
    make_sync_client,
)
from whalefall.llm.postprocess import (
    TokenUtils,
    clean_json,
    clean_main_text,
)


class LLMClient:
    """LLM API 客户端：同步 + 异步流式，tiktoken token 计数。"""

    DEFAULT_MAX_OUTPUT_TOKENS = 4096

    def __init__(self, model: str = "gpt-4o-mini"):
        print(f"\n初始化 LLM 客户端: {model}")
        self.model = model
        self.model_name, self.model_url, self._key = get_model_info(model)
        if not self._key:
            raise RuntimeError(
                f"LLM 配置缺少 api_key: 请在 llm_config.ini 的 [models] 下为 "
                f"{model!r} 配置 {model}_key。"
            )

        self._sync_client = make_sync_client(self.model_url, self._key)
        self._sync_clients_by_key: Dict[Tuple[str, str, str], OpenAI] = {
            client_cache_key(self.model_name, self.model_url, self._key): self._sync_client,
        }
        self.__async_client: Optional[AsyncOpenAI] = None
        self._async_clients_by_key: Dict[Tuple[str, str, str], AsyncOpenAI] = {}
        self._token_utils = TokenUtils()
        print("初始化 LLM 客户端完成")

    # ── 客户端解析 ────────────────────────────────────────────────────────

    def _get_default_async_client(self) -> AsyncOpenAI:
        if self.__async_client is None:
            self.__async_client = make_async_client(self.model_url, self._key)
            self._async_clients_by_key[
                client_cache_key(self.model_name, self.model_url, self._key)
            ] = self.__async_client
        return self.__async_client

    def _get_or_create_async_client(
        self,
        model_name: str,
        model_url: str,
        api_key: str,
    ) -> AsyncOpenAI:
        key = client_cache_key(model_name, model_url, api_key)
        client = self._async_clients_by_key.get(key)
        if client is None:
            client = make_async_client(model_url, api_key)
            self._async_clients_by_key[key] = client
        return client

    def _get_or_create_sync_client(
        self,
        model_name: str,
        model_url: str,
        api_key: str,
    ) -> OpenAI:
        key = client_cache_key(model_name, model_url, api_key)
        client = self._sync_clients_by_key.get(key)
        if client is None:
            client = make_sync_client(model_url, api_key)
            self._sync_clients_by_key[key] = client
        return client

    def _resolve_async_client(self, model: Optional[str]) -> Tuple[str, AsyncOpenAI]:
        if model is not None:
            model_name, model_url, api_key = get_model_info(model)
            return model_name, self._get_or_create_async_client(model_name, model_url, api_key)
        return self.model_name, self._get_default_async_client()

    def _resolve_sync_client(self, model: Optional[str]) -> Tuple[str, OpenAI]:
        if model is not None:
            model_name, model_url, api_key = get_model_info(model)
            return model_name, self._get_or_create_sync_client(model_name, model_url, api_key)
        return self.model_name, self._sync_client

    # ── 同步单轮 ──────────────────────────────────────────────────────────

    def call_llm(
        self,
        prompt: str,
        model: Optional[str] = None,
        timeout: int = 180,
        max_tokens: Optional[int] = None,
        clean_json: bool = False,
        system_message: Optional[str] = None,
    ) -> str:
        model_name, client = self._resolve_sync_client(model)
        kwargs: Dict[str, Any] = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_message or "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
            "timeout": timeout,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        resp = client.chat.completions.create(**kwargs)
        result = (completion_first_message(resp).content or "").strip()

        if not clean_json:
            return result
        cleaned = self._clean_json(result)
        try:
            return json.loads(cleaned)
        except (json.JSONDecodeError, ValueError) as e:
            raise Exception(f"JSON 解析失败: {e}") from e

    # ── 异步单轮 ──────────────────────────────────────────────────────────

    async def call_llm_async(
        self,
        prompt: str,
        model: Optional[str] = None,
        timeout: int = 120,
        max_tokens: Optional[int] = None,
        system_message: Optional[str] = None,
    ) -> str:
        model_name, client = self._resolve_async_client(model)
        kwargs: Dict[str, Any] = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_message or "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
            "timeout": timeout,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        resp = await client.chat.completions.create(**kwargs)
        return (completion_first_message(resp).content or "").strip()

    # ── 异步流式工具对话 ───────────────────────────────────────────────

    async def stream_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        timeout: int = 300,
        chunk_timeout: int = 120,
        max_tokens: Optional[int] = None,
    ) -> AsyncGenerator[Union[str, List[Dict[str, Any]]], None]:
        """
        异步流式工具对话。

        Yields:
          str                     — 文本 delta（每个 token 片段，实时推送）
          List[Dict[str, Any]]    — 完整 tool_calls（最后一次 yield，标志调用结束）

        调用方约定：
          async for item in llm.stream_with_tools(...):
              if isinstance(item, str):
                  # 实时文本 delta
              else:
                  tool_calls = item  # 调用结束，tool_calls 可能为空 list

        超时策略（双层）：
          chunk_timeout — 每个 chunk 到达的最长等待，防止单 chunk 卡死
          timeout       — 整个流的 wall-clock 上限，防止 keep-alive 空 chunk 无限续期
        """
        model_name, client = self._resolve_async_client(model)
        kwargs: Dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "tools": tools or [],
            "timeout": timeout,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        tool_calls_acc: Dict[int, Dict[str, Any]] = {}

        try:
            stream = await asyncio.wait_for(
                client.chat.completions.create(**kwargs, stream=True),
                timeout=timeout,
            )
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError(
                        f"LLM 流式响应总时长超过 {timeout}s（wall-clock timeout）"
                    )
                try:
                    chunk = await asyncio.wait_for(
                        stream.__anext__(),
                        timeout=min(chunk_timeout, remaining),
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    raise asyncio.TimeoutError(
                        f"LLM 流式响应超过 {chunk_timeout}s 无数据（chunk_timeout）"
                    )
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                if delta.content:
                    content_delta = delta.content
                    if isinstance(content_delta, str):
                        yield content_delta
                    elif isinstance(content_delta, list):
                        for part in content_delta:
                            if isinstance(part, dict):
                                text = str(part.get("text", ""))
                            else:
                                text = str(getattr(part, "text", "") or "")
                            if text:
                                yield text

                for tc in delta.tool_calls or []:
                    idx = tc.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                    acc = tool_calls_acc[idx]
                    if tc.id:
                        acc["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            acc["name"] += tc.function.name
                        if tc.function.arguments:
                            acc["arguments"] += tc.function.arguments

        except asyncio.TimeoutError:
            raise
        except Exception:
            # 流式失败：降级非流式
            kwargs_ns = dict(kwargs)
            resp = await asyncio.wait_for(
                client.chat.completions.create(**kwargs_ns), timeout=timeout
            )
            msg = completion_first_message(resp)
            if msg.content:
                fallback_content = msg.content
                if isinstance(fallback_content, str):
                    yield fallback_content
                elif isinstance(fallback_content, list):
                    for part in fallback_content:
                        if isinstance(part, dict):
                            text = str(part.get("text", "") or part.get("content", ""))
                        else:
                            text = str(
                                getattr(part, "text", "")
                                or getattr(part, "content", "")
                                or ""
                            )
                        if text:
                            yield text
                else:
                    yield str(fallback_content)
            for i, tc in enumerate(msg.tool_calls or []):
                args = tc.function.arguments or "{}"
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                tool_calls_acc[i] = {
                    "id": tc.id or "",
                    "name": tc.function.name or "",
                    "arguments": args,
                }

        tool_calls = [
            {
                "id": tool_calls_acc[i]["id"],
                "type": "function",
                "function": {
                    "name": tool_calls_acc[i]["name"],
                    "arguments": tool_calls_acc[i]["arguments"],
                },
            }
            for i in sorted(tool_calls_acc)
            if tool_calls_acc[i]["name"]
        ]
        yield tool_calls

    # ── Token / 文本后处理（门面委托到 postprocess/）────────────────────

    def count_tokens(self, text: str) -> int:
        return self._token_utils.count(text)

    def truncate_by_tokens(self, text: str, max_tokens: int) -> str:
        return self._token_utils.truncate(text, max_tokens)

    def truncate_head_tail(self, text: str, max_tokens: int, head_ratio: float = 0.7) -> str:
        return self._token_utils.truncate_head_tail(text, max_tokens, head_ratio)

    def _clean_json(self, response: str) -> str:
        return clean_json(response)

    def clean_main_text(
        self,
        raw: str,
        strong_keywords: Optional[List[str]] = None,
        end_section_keywords: Optional[List[str]] = None,
        weak_keywords: Optional[List[str]] = None,
        end_section_threshold: float = 0.7,
        weak_keywords_threshold: float = 0.8,
        header_footer_min_count: int = 10,
        header_footer_max_length: int = 80,
    ) -> str:
        return clean_main_text(
            raw,
            strong_keywords=strong_keywords,
            end_section_keywords=end_section_keywords,
            weak_keywords=weak_keywords,
            end_section_threshold=end_section_threshold,
            weak_keywords_threshold=weak_keywords_threshold,
            header_footer_min_count=header_footer_min_count,
            header_footer_max_length=header_footer_max_length,
        )


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="快速测 OpenAI-compatible 接口是否通")
    p.add_argument("--model", default="gpt-4o", help="llm_config.ini 里配置的别名")
    p.add_argument("--timeout", type=int, default=60)
    args = p.parse_args()

    try:
        client = LLMClient(model=args.model)
        out = client.call_llm(
            '仅用一句话回复单词 "pong"。不要其它解释。',
            timeout=args.timeout,
            max_tokens=32,
        )
    except Exception as e:
        print(f"[失败] {type(e).__name__}: {e}")
        raise SystemExit(1) from e
    print("[ok]", out)
