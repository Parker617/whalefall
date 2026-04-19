"""
最近活跃会话 id 的小工具（仅保存一个字符串）。

用途：CLI `--resume-last` / `/resume-last` 斜杠命令，让用户一键跳回上次
会话，对齐 Web 端 `localStorage` 的行为。

持久化位置：
  `~/.whalefall/runtime/state/last_session.txt`

可被环境变量 `WHALEFALL_LAST_SESSION_FILE` 覆盖（测试或沙箱部署用）。

设计原则：
- 纯文件读写，不依赖 SessionStore（避免循环依赖）
- 任何异常都吞掉并返回 None/False，**绝不**因为状态文件故障影响主流程
- 写入时只存 sid 字面量，自带换行
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


_DEFAULT_PATH = Path.home() / ".whalefall" / "runtime" / "state" / "last_session.txt"


def _resolve_path() -> Path:
    env = os.environ.get("WHALEFALL_LAST_SESSION_FILE")
    if env:
        return Path(env).expanduser()
    return _DEFAULT_PATH


def record_last_session(session_id: str) -> bool:
    """落盘最近活跃会话 id。失败不抛异常，返回 False。"""
    sid = (session_id or "").strip()
    if not sid:
        return False
    path = _resolve_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sid + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def read_last_session() -> Optional[str]:
    """读最近活跃会话 id。无文件/非法内容返回 None。"""
    path = _resolve_path()
    try:
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8").strip()
        return raw or None
    except OSError:
        return None


def clear_last_session() -> bool:
    """删除 last_session 记录（/clear 或 /drop 时调用）。"""
    path = _resolve_path()
    try:
        if path.exists():
            path.unlink()
        return True
    except OSError:
        return False
