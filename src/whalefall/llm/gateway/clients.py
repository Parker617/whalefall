"""
OpenAI / AsyncOpenAI 客户端工厂 + URL 归一化 + 缓存 key。

下游只需 `make_sync_client` / `make_async_client` 两个入口。
"""
from __future__ import annotations

from typing import Tuple

from openai import AsyncOpenAI, OpenAI


def normalize_base_url(url: str) -> str:
    """去掉末尾的 `/chat/completions`，OpenAI SDK 会自动追加。"""
    base = (url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base


def client_cache_key(model_name: str, model_url: str, api_key: str) -> Tuple[str, str, str]:
    """按 (模型名, 归一化 URL, api_key) 做客户端缓存 key。"""
    return (model_name, normalize_base_url(model_url), api_key or "")


def make_sync_client(model_url: str, api_key: str) -> OpenAI:
    """用给定的 api_key 构造 OpenAI 同步客户端。"""
    if not api_key:
        raise ValueError(
            "api_key 为空。请在 llm_config.ini 里为该模型配置 {alias}_key。"
        )
    return OpenAI(base_url=normalize_base_url(model_url), api_key=api_key)


def make_async_client(model_url: str, api_key: str) -> AsyncOpenAI:
    """异步版本的 `make_sync_client`。"""
    if not api_key:
        raise ValueError(
            "api_key 为空。请在 llm_config.ini 里为该模型配置 {alias}_key。"
        )
    return AsyncOpenAI(base_url=normalize_base_url(model_url), api_key=api_key)
