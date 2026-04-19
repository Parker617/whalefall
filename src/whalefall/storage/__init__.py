"""
whalefall.storage —— 持久化与运行时落盘层。

集中管理：
  - session_store ：对话 session 的 SQLite 持久化（per-session 锁）
  - trace         ：结构化 JSONL trace（每 rid 一文件）+ 大产物 artifact
  - retention     ：运行时目录容量/TTL 清理（logs/traces/transcripts/artifacts ...）

所有落盘路径均受 whalefall.core.runtime.runtime_root() 约束，
默认位于 src/whalefall/.runtime/ 下，不会逸出项目目录。
"""
from whalefall.storage.retention import RuntimeRetention
from whalefall.storage.session_store import SessionStore
from whalefall.storage.trace import TraceWriter, clean_traces

__all__ = [
    "RuntimeRetention",
    "SessionStore",
    "TraceWriter",
    "clean_traces",
]
