"""
tiktoken 封装：token 计数与头尾截断。

主要给 `ContextManager` 与 `LLMClient` 用；其它地方按需创建 `TokenUtils()`。
"""
from __future__ import annotations

import tiktoken


class TokenUtils:
    """`cl100k_base` 编码的轻量封装。"""

    def __init__(self, encoding_name: str = "cl100k_base"):
        self.encoding = tiktoken.get_encoding(encoding_name)

    def count(self, text: str) -> int:
        return len(self.encoding.encode(text or ""))

    def truncate(self, text: str, max_tokens: int) -> str:
        """按 token 数截断到前 `max_tokens` 个，超过则截掉尾部。"""
        tokens = self.encoding.encode(text or "")
        if len(tokens) <= max_tokens:
            return text
        return self.encoding.decode(tokens[:max_tokens])

    def truncate_head_tail(
        self,
        text: str,
        max_tokens: int,
        head_ratio: float = 0.7,
    ) -> str:
        """保留头部 head_ratio 比例、尾部剩余（中间塞一段省略标记）。"""
        tokens = self.encoding.encode(text or "")
        n = len(tokens)
        if n <= max_tokens:
            return text
        head_tokens = int(max_tokens * head_ratio)
        tail_tokens = max_tokens - head_tokens - 20
        if tail_tokens <= 0:
            return self.encoding.decode(tokens[:max_tokens])
        marker = self.encoding.encode("\n\n[... 中间内容已截断 ...]\n\n")
        return self.encoding.decode(tokens[:head_tokens] + marker + tokens[-tail_tokens:])
