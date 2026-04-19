"""
运行态目录管理（统一放在 whalefall/.runtime 下）。

目标：
- 让日志、trace、artifact、状态文件都落在项目内，不污染外层目录。
- 避免各模块各自拼路径导致不一致。
"""
from __future__ import annotations

import os
from pathlib import Path


def package_root() -> Path:
    """whalefall 包根目录。"""
    return Path(__file__).resolve().parents[1]


def runtime_root() -> Path:
    """
    运行态根目录。
    默认：whalefall/.runtime
    可选覆盖：WHALEFALL_RUNTIME_DIR=/abs/path
    """
    raw = (os.getenv("WHALEFALL_RUNTIME_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return package_root() / ".runtime"


def logs_dir() -> Path:
    return runtime_root() / "logs"


def traces_dir() -> Path:
    return runtime_root() / "traces"


def artifacts_dir() -> Path:
    return runtime_root() / "artifacts"


def tool_results_dir() -> Path:
    return runtime_root() / "tool_results"


def transcripts_dir() -> Path:
    """全量对话归档目录（每 session 一个 JSONL；永不被 FIFO 削减）。"""
    return runtime_root() / "transcripts"


def state_dir() -> Path:
    return runtime_root() / "state"


def state_db_path() -> Path:
    return state_dir() / "state.db"


def sessions_db_path() -> Path:
    return state_dir() / "sessions.sqlite3"


def ensure_runtime_layout() -> Path:
    """
    确保运行态目录结构存在。
    返回 runtime_root。
    """
    root = runtime_root()
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "traces").mkdir(parents=True, exist_ok=True)
    (root / "artifacts").mkdir(parents=True, exist_ok=True)
    (root / "tool_results").mkdir(parents=True, exist_ok=True)
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "transcripts").mkdir(parents=True, exist_ok=True)
    return root
