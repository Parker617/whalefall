# coding: utf-8
"""
结构化 trace（JSONL 时间线），与 mcp.log（人读时间线）职责分离。

- 每个 request_id（rid）一个文件：.runtime/traces/YYYY-MM-DD/<rid>.jsonl
- 每行一个 JSON 对象，严格按时间顺序 append，不重写整文件
- 大工具结果（> ARTIFACT_THRESHOLD）落 artifact：.runtime/traces/YYYY-MM-DD/<rid>/tool_<id>.txt
  event 里只存 result_len、result_preview、artifact_path

用法（示例）：
    from whalefall.storage.trace import TraceWriter

    tw = TraceWriter(rid=rid, sid="sid", uid="uid", agent="agent", model="gpt-4o", enabled=True)
    tw.set_tools(tools_schema)
    tw.log_system(system_prompt)
    tw.log_user(user_query)
    tw.add_llm_round(1, content, tool_calls)
    tw.add_tool_run(tool_call_id, name, ok=True, latency_ms=123, args=args, result_text=result)
    tw.finish(ok=True, final_text=final_answer)

清理：不在此处做。请用独立脚本或 cron 调 clean_traces()。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from whalefall.core.log import get_logger, redact_any, truncate
from whalefall.core.runtime import traces_dir

_logger = get_logger("whalefall.trace")


def _traces_dir() -> Path:
    return traces_dir()


def ensure_traces_dir(day: Optional[str] = None) -> Path:
    """只负责创建目录，不做任何清理。"""
    base = _traces_dir()
    base.mkdir(parents=True, exist_ok=True)
    if day:
        dd = base / day
        dd.mkdir(parents=True, exist_ok=True)
        return dd
    return base


def clean_traces(max_files: Optional[int] = None) -> None:
    """按 trace 文件数清理，供脚本/cron 用。max_files 默认 200，≤0 不清理。"""
    if max_files is None:
        raw = os.getenv("MCP_TRACE_MAX_FILES", "200")
        max_files = int(raw) if raw else 200
    if max_files <= 0:
        return
    base = _traces_dir()
    if not base.exists():
        return
    try:
        files = sorted(
            [p for p in base.rglob("*.jsonl") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in files[max_files:]:
            try:
                p.unlink(missing_ok=True)
                rid_dir = p.parent / p.stem
                if rid_dir.exists() and rid_dir.is_dir():
                    shutil.rmtree(rid_dir, ignore_errors=True)
            except Exception as exc:
                _logger.warning("clean trace file failed | path=%s err=%s", p, exc)
    except Exception as exc:
        _logger.warning("clean_traces scan failed | err=%s", exc)


def _today_local() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _today_utc() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _utc_ts() -> str:
    return datetime.utcnow().isoformat() + "Z"


def safe_json_dumps(x: Any) -> str:
    try:
        return json.dumps(x, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        try:
            return str(x)
        except Exception:
            return "<unserializable>"


def _sanitize_tool_call_id(tool_call_id: str) -> str:
    s = (tool_call_id or "unknown").strip()
    return re.sub(r"[^\w\-.]", "_", s)[:120]


class TraceWriter:
    """
    JSONL 落盘：每行一个 JSON，append-only。
    首行 type=meta；之后 system / user / assistant / tool / final。
    大 tool result 落 artifact，event 里只存 result_len、result_preview、artifact_path。
    """

    MAX_TEXT_CHARS = int(os.getenv("MCP_TRACE_MAX_TEXT_CHARS", "1200"))
    MAX_ARGS_CHARS = int(os.getenv("MCP_TRACE_MAX_ARGS_CHARS", "800"))
    MAX_RESULT_CHARS = int(os.getenv("MCP_TRACE_MAX_RESULT_CHARS", "1000"))
    ARTIFACT_THRESHOLD = int(os.getenv("MCP_TRACE_ARTIFACT_THRESHOLD", "50000"))

    def __init__(
        self,
        rid: str,
        sid: str = "-",
        uid: str = "-",
        agent: str = "-",
        model: str = "-",
        enabled: Optional[bool] = None,
    ):
        if enabled is None:
            enabled = (os.getenv("MCP_TRACE") or "").lower() in {"1", "true", "yes"}
        self.enabled = bool(enabled)
        self.day = _today_utc()
        self.path = ensure_traces_dir(self.day) / f"{rid}.jsonl"
        self.rid = rid
        self.sid = sid or "-"
        self.uid = uid or "-"
        self.agent = agent or "-"
        self.model = model or "-"
        self.started_at = _utc_ts()
        self._tools: List[str] = []
        self._write_lock = threading.Lock()
        self._meta_written = False

    def _append_line(self, obj: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            ensure_traces_dir(self.day)
            line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
            with self._write_lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception as exc:
            _logger.warning("trace append failed | rid=%s err=%s", self.rid, exc)

    def _artifact_dir(self) -> Path:
        d = _traces_dir() / self.day / self.rid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _ensure_meta(self) -> None:
        """首行写 meta（lazy-init），忘调 set_tools 时也会有一行 meta。"""
        if not self.enabled or self._meta_written:
            return
        meta = {
            "type": "meta",
            "rid": self.rid,
            "sid": self.sid,
            "uid": self.uid,
            "agent": self.agent,
            "model": self.model,
            "tools": self._tools,
            "started_at": self.started_at,
        }
        self._append_line(meta)
        self._meta_written = True

    def set_tools(self, tools_schema: List[Dict[str, Any]]) -> None:
        if not self.enabled:
            return
        self._tools = []
        for t in tools_schema or []:
            fn = (t or {}).get("function") or {}
            n = fn.get("name")
            if n:
                self._tools.append(str(n))
        self._ensure_meta()

    def log_system(self, content: str) -> None:
        if not self.enabled:
            return
        self._ensure_meta()
        self._append_line({
            "type": "system",
            "ts": _utc_ts(),
            "content": truncate(content or "", self.MAX_TEXT_CHARS),
        })

    def log_user(self, content: str) -> None:
        if not self.enabled:
            return
        self._ensure_meta()
        self._append_line({
            "type": "user",
            "ts": _utc_ts(),
            "content": truncate(content or "", self.MAX_TEXT_CHARS),
        })

    def add_llm_round(self, i: int, content: str, tool_calls: Optional[List[Dict[str, Any]]]) -> None:
        if not self.enabled:
            return
        self._ensure_meta()
        calls: List[Dict[str, Any]] = []
        for tc in tool_calls or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            args_raw = fn.get("arguments", {})
            args_obj = redact_any(args_raw) if isinstance(args_raw, (dict, list)) else args_raw
            args_dump = safe_json_dumps(args_obj)
            calls.append({
                "id": tc.get("id", ""),
                "name": str(name),
                "arguments_preview": truncate(args_dump, self.MAX_ARGS_CHARS),
            })
        self._append_line({
            "type": "assistant",
            "ts": _utc_ts(),
            "round": int(i),
            "content": truncate(content or "", self.MAX_TEXT_CHARS),
            "tool_calls": calls,
        })

    def add_tool_run(
        self,
        tool_call_id: str,
        name: str,
        ok: bool,
        latency_ms: int,
        args: Dict[str, Any],
        result_text: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        self._ensure_meta()
        args_dump = safe_json_dumps(redact_any(args or {}))
        result_len = len(result_text or "") if result_text is not None else 0
        result_preview = truncate(result_text or "", self.MAX_RESULT_CHARS) if result_text else None

        artifact_path: Optional[str] = None
        if result_text is not None and result_len > self.ARTIFACT_THRESHOLD:
            sid = _sanitize_tool_call_id(tool_call_id)
            ts = datetime.utcnow().strftime("%H%M%S%f")
            fname = f"tool_{sid}_{ts}.txt"
            art_path = self._artifact_dir() / fname
            try:
                art_path.write_text(result_text, encoding="utf-8")
            except Exception as exc:
                _logger.warning(
                    "trace artifact write failed | rid=%s file=%s err=%s",
                    self.rid, fname, exc,
                )
            else:
                artifact_path = f"{self.day}/{self.rid}/{fname}"

        ev: Dict[str, Any] = {
            "type": "tool",
            "ts": _utc_ts(),
            "tool_call_id": tool_call_id or "",
            "name": str(name),
            "ok": bool(ok),
            "latency_ms": int(latency_ms),
            "args_preview": truncate(args_dump, self.MAX_ARGS_CHARS),
            "result_len": result_len,
            "result_preview": result_preview,
            "error": error,
        }
        if artifact_path is not None:
            ev["artifact_path"] = artifact_path
        self._append_line(ev)

    def finish(self, ok: bool, final_text: str, error: Optional[str] = None) -> None:
        if not self.enabled:
            return
        self._ensure_meta()
        ended = _utc_ts()
        self._append_line({
            "type": "final",
            "ts": ended,
            "content": truncate(final_text or "", self.MAX_TEXT_CHARS),
            "ok": bool(ok),
            "error": error,
        })


class NullTraceWriter(TraceWriter):
    """不落盘，所有写操作为 no-op。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.enabled = False

    def _append_line(self, obj: Dict[str, Any]) -> None:
        return
