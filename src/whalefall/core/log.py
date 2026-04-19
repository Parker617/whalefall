"""
轻量日志：写入 src/whalefall/.runtime/logs/ 下的文件，目标是“可复盘一次请求发生了什么”。

原则：
- INFO：请求开始/结束、每轮 LLM 概况、工具开始/结束（含耗时与成功失败）
- WARNING：工具返回 error、解析失败、有兜底但不理想
- ERROR：不可恢复错误
- DEBUG：锁等待、args/result 截断内容、messages 结构统计

注意：不要全量打印 messages / 工具结果（会爆日志、可能泄露）。
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

from whalefall.core.runtime import logs_dir

def _logs_dir() -> Path:
    return logs_dir()


def ensure_logs_dir() -> Path:
    d = _logs_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_request_id(prefix: str = "rid") -> str:
    # 短一些便于 grep/肉眼排查
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def truncate(s: Any, n: int = 200) -> str:
    if s is None:
        return ""
    t = str(s).replace("\n", " ")
    return t if len(t) <= n else t[:n] + "…"


_DEFAULT_REDACT_KEYS = {
    "token",
    "access_token",
    "refresh_token",
    "app_secret",
    "app_id",
    "authorization",
    "api_key",
    "x-api-key",
    "password",
    "secret",
}


def _normalize_key_for_redact(k: str) -> str:
    return str(k).lower().replace("-", "_")


def redact_any(x: Any, redact_keys: Optional[set[str]] = None) -> Any:
    """递归脱敏：dict/list 递归，其余原样。key 匹配时忽略大小写与连字符（如 x-api-key）。"""
    keys = redact_keys or _DEFAULT_REDACT_KEYS
    keys_norm = {_normalize_key_for_redact(k) for k in keys}
    if isinstance(x, dict):
        out: Dict[str, Any] = {}
        for k, v in x.items():
            if _normalize_key_for_redact(k) in keys_norm:
                out[k] = "***"
            else:
                out[k] = redact_any(v, redact_keys)
        return out
    if isinstance(x, list):
        return [redact_any(i, redact_keys) for i in x]
    return x


def redact_dict(d: Dict[str, Any], redact_keys: Optional[set[str]] = None) -> Dict[str, Any]:
    """脱敏 dict（递归）；兼容旧调用。"""
    return redact_any(d, redact_keys)  # type: ignore[return-value]

def safe_json_dumps(obj: Any) -> str:
    """日志用序列化：尽量 json，失败就退化为 str(obj)，避免日志本身炸主流程。"""
    try:
        import json as _json  # 避免污染外部命名

        return _json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        try:
            return str(obj)
        except Exception:
            return "<unprintable>"


class DefaultCtxFilter(logging.Filter):
    """为每条 record 补默认 rid/sid/uid/agent，避免 formatter 里 %(rid)s 等缺字段导致 KeyError。"""

    def filter(self, record: logging.LogRecord) -> bool:
        for k in ("rid", "sid", "uid", "agent"):
            if not hasattr(record, k):
                setattr(record, k, "-")
        return True


class _CtxAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        extra = kwargs.get("extra") or {}
        merged = {**self.extra, **extra}
        kwargs["extra"] = merged
        return msg, kwargs


def get_logger(
    name: str = "whalefall.mcp",
    log_file: str = "mcp.log",
    level: Optional[str] = None,
) -> logging.Logger:
    """
    返回一个带文件输出的 logger（单例风格：同名 logger 只初始化一次 handler）。
    默认文件：src/whalefall/.runtime/logs/mcp.log（滚动）。
    """
    logger = logging.getLogger(name)
    if getattr(logger, "_mcp_inited", False):
        return logger

    ensure_logs_dir()
    file_path = _logs_dir() / log_file

    lvl = (level or os.getenv("MCP_LOG_LEVEL") or "INFO").upper()
    logger.setLevel(getattr(logging, lvl, logging.INFO))
    logger.propagate = False

    fmt = (
        "%(asctime)s | %(levelname)s | rid=%(rid)s sid=%(sid)s uid=%(uid)s agent=%(agent)s | %(message)s"
    )
    formatter = logging.Formatter(fmt)

    logger.addFilter(DefaultCtxFilter())
    fh = RotatingFileHandler(
        file_path,
        maxBytes=50 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    fh.setLevel(logger.level)
    logger.addHandler(fh)

    # 可选：控制台输出（默认关）
    # 支持两种模式：普通 StreamHandler 或 Rich 彩色输出
    stdout_mode = (os.getenv("MCP_LOG_STDOUT") or "").lower()
    if stdout_mode in {"1", "true", "yes"}:
        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        sh.setLevel(logger.level)
        logger.addHandler(sh)
    elif stdout_mode in {"rich", "color"}:
        # Rich 彩色控制台输出
        try:
            from rich.logging import RichHandler
            rh = RichHandler(
                rich_tracebacks=True,
                markup=True,
                show_path=False,
                keywords=["tool start", "tool end", "llm round", "request start", "request end"],
            )
            rh.setLevel(logger.level)
            logger.addHandler(rh)
        except ImportError:
            # Rich 未安装，fallback 到普通 StreamHandler
            sh = logging.StreamHandler()
            sh.setFormatter(formatter)
            sh.setLevel(logger.level)
            logger.addHandler(sh)

    logger._mcp_inited = True  # type: ignore[attr-defined]
    return logger


def get_request_logger(
    base_logger: logging.Logger,
    request_id: str,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> logging.LoggerAdapter:
    return _CtxAdapter(
        base_logger,
        {
            "rid": request_id,
            "sid": session_id or "-",
            "uid": user_id or "-",
            "agent": agent_name or "-",
        },
    )


class Timer:
    def __init__(self):
        self._t0 = time.perf_counter()

    def ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)
