"""
whalefall.llm.postprocess —— LLM 输出的后处理工具。

拆分原则：
  - json_cleaner ：从"杂字文本"里挖出合法 JSON（代码块剥离、未转义引号修补等）
  - text_cleaner ：长文本清洗（页眉页脚去重、尾部声明截断等）
  - tokens       ：tiktoken 包装（count / truncate / head_tail）

所有函数纯函数化，便于单测与在 `LLMClient` 之外独立复用。
"""
from whalefall.llm.postprocess.json_cleaner import (
    clean_json,
    extract_json_like,
    fix_unescaped_quotes,
)
from whalefall.llm.postprocess.text_cleaner import clean_main_text
from whalefall.llm.postprocess.tokens import TokenUtils

__all__ = [
    "TokenUtils",
    "clean_json",
    "clean_main_text",
    "extract_json_like",
    "fix_unescaped_quotes",
]
