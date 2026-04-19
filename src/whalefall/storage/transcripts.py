"""
全量对话归档（transcripts）：按 session_id 追加 JSONL，不做任何删减。

动机：
- SQLite 里的 session_messages 有 FIFO 条数上限（防磁盘无限膨胀）。
- 自动压缩（autocompact）时旧消息会被 LLM 摘要替换，信息有损。
- 某些审计/复盘场景希望能查到最原始的每一条 user/assistant/tool 文本。

所以额外加一份"只进不出"的 JSONL 存档。文件按 session_id 命名，一行一条
消息，行末换行。存档由 `RuntimeRetention` 按容量/TTL 统一清理。

线程安全：文件级 append + os.fsync，不加锁（多进程同时写同一 session
才会产生交织，通常场景下 session-id 与单进程 CLI/Web 一一对应）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable

from whalefall.core.runtime import transcripts_dir


_SAFE_SID_RE = re.compile(r"[^A-Za-z0-9._\-]")


def _safe_sid_filename(session_id: str) -> str:
    """把任意 session_id 转成安全文件名，保留可读性。"""
    return _SAFE_SID_RE.sub("_", (session_id or "default"))[:80] or "default"


def transcripts_path(session_id: str) -> Path:
    """返回指定 session 的 transcripts 文件路径（不保证存在）。"""
    return transcripts_dir() / f"{_safe_sid_filename(session_id)}.jsonl"


def append_transcript(session_id: str, msg: Dict[str, Any]) -> bool:
    """追加一条消息。失败返回 False（绝不抛异常影响主流程）。"""
    if not isinstance(msg, dict):
        return False
    try:
        path = transcripts_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(msg, ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


def append_transcript_batch(session_id: str, msgs: Iterable[Dict[str, Any]]) -> int:
    """批量追加；返回成功写入的条数。"""
    count = 0
    for m in msgs:
        if append_transcript(session_id, m):
            count += 1
    return count


__all__ = [
    "append_transcript",
    "append_transcript_batch",
    "transcripts_path",
]
