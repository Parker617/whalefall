"""
whalefall.llm.gateway —— LLM 网关底层：客户端工厂 + 响应校验。

拆分原则：
  - clients  ：OpenAI / AsyncOpenAI 的工厂、URL 归一化、缓存 key
  - response ：ChatCompletion 响应校验（空 choices / 业务错误）

`LLMClient`（llm/llm_client.py）组合这几个模块，暴露业务 API。
"""
from whalefall.llm.gateway.clients import (
    client_cache_key,
    make_async_client,
    make_sync_client,
    normalize_base_url,
)
from whalefall.llm.gateway.response import completion_first_message

__all__ = [
    "client_cache_key",
    "completion_first_message",
    "make_async_client",
    "make_sync_client",
    "normalize_base_url",
]
