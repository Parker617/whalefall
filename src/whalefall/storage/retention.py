# coding: utf-8
"""
RuntimeRetention：运行态容量治理。

清理范围：
- traces：按文件数量 + 总体积
- artifacts：按总体积（LRU 删除最旧文件）
- sessions：由 SessionStore.enforce_limits() 处理
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from whalefall.core.log import get_logger
from whalefall.core.runtime import artifacts_dir, runtime_root, tool_results_dir, traces_dir
from whalefall.storage.trace import clean_traces

_logger = get_logger("whalefall.retention")

DEFAULT_TRANSCRIPTS_MAX_BYTES = 256 * 1024 * 1024   # 256MB
DEFAULT_LOGS_MAX_BYTES = 256 * 1024 * 1024          # 256MB

DEFAULT_TRACES_MAX_FILES = 1000
DEFAULT_TRACES_MAX_BYTES = 1024 * 1024 * 1024      # 1GB
DEFAULT_ARTIFACTS_MAX_BYTES = 1024 * 1024 * 1024   # 1GB
DEFAULT_TOOL_RESULTS_MAX_BYTES = 512 * 1024 * 1024  # 512MB
DEFAULT_MIN_FILE_AGE_SEC = 120


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _iter_files(root: Path) -> List[Path]:
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file()]


def _prune_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    # 先深后浅
    for d in sorted([p for p in root.rglob("*") if p.is_dir()], key=lambda x: len(x.parts), reverse=True):
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except Exception as exc:
            _logger.debug("prune empty dir failed | path=%s err=%s", d, exc)


def _trim_dir_lru(
    root: Path,
    *,
    max_bytes: int,
    min_file_age_sec: int = DEFAULT_MIN_FILE_AGE_SEC,
) -> Tuple[int, int]:
    """
    LRU 删除目录文件直到体积 <= max_bytes（仅删“足够旧”的文件）。
    返回：(deleted_files, deleted_bytes)
    """
    if max_bytes <= 0 or not root.exists():
        return 0, 0

    files = _iter_files(root)
    if not files:
        return 0, 0

    total = 0
    file_infos: List[Tuple[Path, float, int]] = []  # (path, mtime, size)
    now = time.time()
    for p in files:
        try:
            st = p.stat()
        except Exception:
            continue
        size = int(st.st_size)
        total += size
        file_infos.append((p, float(st.st_mtime), size))

    if total <= max_bytes:
        return 0, 0

    deleted_files = 0
    deleted_bytes = 0
    # 最旧优先删除
    file_infos.sort(key=lambda x: x[1])
    for p, mtime, size in file_infos:
        if total <= max_bytes:
            break
        # 避免删到刚写入文件
        if now - mtime < max(0, int(min_file_age_sec)):
            continue
        try:
            p.unlink(missing_ok=True)
        except Exception:
            continue
        total -= size
        deleted_files += 1
        deleted_bytes += size

    _prune_empty_dirs(root)
    return deleted_files, deleted_bytes


class RuntimeRetention:
    """统一运行态清理入口。"""

    def __init__(
        self,
        *,
        traces_max_files: Optional[int] = None,
        traces_max_bytes: Optional[int] = None,
        artifacts_max_bytes: Optional[int] = None,
        tool_results_max_bytes: Optional[int] = None,
        transcripts_max_bytes: Optional[int] = None,
        logs_max_bytes: Optional[int] = None,
        min_file_age_sec: int = DEFAULT_MIN_FILE_AGE_SEC,
    ):
        self.traces_max_files = (
            _env_int("WHALEFALL_RETENTION_TRACES_MAX_FILES", DEFAULT_TRACES_MAX_FILES)
            if traces_max_files is None else int(traces_max_files)
        )
        self.traces_max_bytes = (
            _env_int("WHALEFALL_RETENTION_TRACES_MAX_BYTES", DEFAULT_TRACES_MAX_BYTES)
            if traces_max_bytes is None else int(traces_max_bytes)
        )
        self.artifacts_max_bytes = (
            _env_int("WHALEFALL_RETENTION_ARTIFACTS_MAX_BYTES", DEFAULT_ARTIFACTS_MAX_BYTES)
            if artifacts_max_bytes is None else int(artifacts_max_bytes)
        )
        self.tool_results_max_bytes = (
            _env_int("WHALEFALL_RETENTION_TOOL_RESULTS_MAX_BYTES", DEFAULT_TOOL_RESULTS_MAX_BYTES)
            if tool_results_max_bytes is None else int(tool_results_max_bytes)
        )
        self.transcripts_max_bytes = (
            _env_int("WHALEFALL_RETENTION_TRANSCRIPTS_MAX_BYTES", DEFAULT_TRANSCRIPTS_MAX_BYTES)
            if transcripts_max_bytes is None else int(transcripts_max_bytes)
        )
        self.logs_max_bytes = (
            _env_int("WHALEFALL_RETENTION_LOGS_MAX_BYTES", DEFAULT_LOGS_MAX_BYTES)
            if logs_max_bytes is None else int(logs_max_bytes)
        )
        self.min_file_age_sec = max(0, int(min_file_age_sec))

    def run(self, *, session_store=None) -> Dict[str, int]:
        """
        执行一次清理，返回统计。
        """
        stats = {
            "sessions_deleted": 0,
            "sessions_trimmed": 0,
            "traces_deleted_files": 0,
            "traces_deleted_bytes": 0,
            "artifacts_deleted_files": 0,
            "artifacts_deleted_bytes": 0,
            "tool_results_deleted_files": 0,
            "tool_results_deleted_bytes": 0,
            "transcripts_deleted_files": 0,
            "transcripts_deleted_bytes": 0,
            "logs_deleted_files": 0,
            "logs_deleted_bytes": 0,
        }

        # 1) traces：按文件数量
        try:
            clean_traces(max_files=self.traces_max_files)
        except Exception as exc:
            _logger.warning("clean_traces failed | err=%s", exc)

        # 2) traces：按总大小
        try:
            d_files, d_bytes = _trim_dir_lru(
                traces_dir(),
                max_bytes=max(0, self.traces_max_bytes),
                min_file_age_sec=self.min_file_age_sec,
            )
            stats["traces_deleted_files"] = d_files
            stats["traces_deleted_bytes"] = d_bytes
        except Exception as exc:
            _logger.warning("trim traces failed | err=%s", exc)

        # 3) artifacts：按总大小
        try:
            d_files, d_bytes = _trim_dir_lru(
                artifacts_dir(),
                max_bytes=max(0, self.artifacts_max_bytes),
                min_file_age_sec=self.min_file_age_sec,
            )
            stats["artifacts_deleted_files"] = d_files
            stats["artifacts_deleted_bytes"] = d_bytes
        except Exception as exc:
            _logger.warning("trim artifacts failed | err=%s", exc)

        # 4) tool_results：按总大小
        try:
            d_files, d_bytes = _trim_dir_lru(
                tool_results_dir(),
                max_bytes=max(0, self.tool_results_max_bytes),
                min_file_age_sec=self.min_file_age_sec,
            )
            stats["tool_results_deleted_files"] = d_files
            stats["tool_results_deleted_bytes"] = d_bytes
        except Exception as exc:
            _logger.warning("trim tool_results failed | err=%s", exc)

        # 5) transcripts：子 agent 全量对话备份，按总大小 LRU 清理
        try:
            d_files, d_bytes = _trim_dir_lru(
                runtime_root() / "transcripts",
                max_bytes=max(0, self.transcripts_max_bytes),
                min_file_age_sec=self.min_file_age_sec,
            )
            stats["transcripts_deleted_files"] = d_files
            stats["transcripts_deleted_bytes"] = d_bytes
        except Exception as exc:
            _logger.warning("trim transcripts failed | err=%s", exc)

        # 6) logs：按总大小 LRU 清理（不含当前日志；日志滚动由 logger 自身负责）
        try:
            d_files, d_bytes = _trim_dir_lru(
                runtime_root() / "logs",
                max_bytes=max(0, self.logs_max_bytes),
                min_file_age_sec=self.min_file_age_sec,
            )
            stats["logs_deleted_files"] = d_files
            stats["logs_deleted_bytes"] = d_bytes
        except Exception as exc:
            _logger.warning("trim logs failed | err=%s", exc)

        # 7) session store：由其自身限流逻辑处理
        if session_store is not None and hasattr(session_store, "enforce_limits"):
            try:
                s = session_store.enforce_limits()
                stats["sessions_deleted"] = int(
                    (s.get("deleted_ttl", 0))
                    + (s.get("deleted_overflow_sessions", 0))
                    + (s.get("deleted_for_size", 0))
                )
                stats["sessions_trimmed"] = int(s.get("trimmed_sessions", 0))
            except Exception as exc:
                _logger.warning("session store enforce_limits failed | err=%s", exc)

        return stats

