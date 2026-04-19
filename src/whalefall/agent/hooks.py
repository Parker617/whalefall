# coding: utf-8
"""
HookManager：主循环钩子管理器。

支持 hook 事件：
  before_llm / after_llm          — LLM 调用前后（可改写 messages/tools/model / content/tool_calls）
  before_tool / after_tool        — 工具执行前后（可改写 args / content）
  on_error                        — 异常发生时（不改变执行流程）
  session_start                   — 会话启动（可注入额外上下文）
  subagent_start                  — 子 Agent 启动（可注入额外上下文到子 Agent）
  tool_use_failure                — 工具执行失败（is_error=True 时触发，用于监控）

约定：hook 接收 dict payload，返回 dict 作为新 payload，返回 None 不修改。
"""
from __future__ import annotations

import traceback
import threading
from typing import Any, Callable, Dict, List, Optional

from whalefall.core.log import get_logger, truncate

HOOK_BEFORE_LLM  = "before_llm"
HOOK_AFTER_LLM   = "after_llm"
HOOK_BEFORE_TOOL = "before_tool"
HOOK_AFTER_TOOL  = "after_tool"
HOOK_ON_ERROR    = "on_error"
HOOK_SESSION_START    = "session_start"       # 会话启动（主循环第一次进入前）
HOOK_SUBAGENT_START   = "subagent_start"      # 子 Agent 启动（spawn 后、loop 前）
HOOK_TOOL_USE_FAILURE = "tool_use_failure"    # 工具执行失败（is_error=True 时）

SUPPORTED_HOOKS = {
    HOOK_BEFORE_LLM, HOOK_AFTER_LLM,
    HOOK_BEFORE_TOOL, HOOK_AFTER_TOOL,
    HOOK_ON_ERROR,
    HOOK_SESSION_START, HOOK_SUBAGENT_START, HOOK_TOOL_USE_FAILURE,
}

HookFunc = Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]
_logger = get_logger("whalefall.hooks")


class HookManager:
    """轻量 hook 注册与触发（线程安全）。"""

    def __init__(self, enable_default_error_hook: bool = True):
        self._hooks: Dict[str, List[HookFunc]] = {name: [] for name in SUPPORTED_HOOKS}
        self._lock = threading.RLock()
        if enable_default_error_hook:
            self.register(HOOK_ON_ERROR, self._default_error_logging_hook)

    @staticmethod
    def _default_error_logging_hook(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        默认 on_error hook：输出结构化错误日志。
        只记录，不修改 payload。
        """
        if not isinstance(payload, dict):
            return payload

        err = payload.get("error")
        if err is None:
            err_text = str(payload.get("message", "unknown error"))
        else:
            err_text = f"{type(err).__name__}: {err}"

        stage = str(payload.get("stage", "unknown"))
        rid = str(payload.get("request_id", "-"))
        step = payload.get("step", "-")
        agent_cfg = payload.get("agent_config")
        agent_name = getattr(agent_cfg, "name", "-")

        _logger.warning(
            "default_on_error_hook | rid=%s agent=%s step=%s stage=%s err=%s",
            rid, agent_name, step, stage, truncate(err_text, 300),
        )
        return payload

    @staticmethod
    def _tool_metrics_hook(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """after_tool hook：记录工具名、结果长度、是否出错。"""
        if not isinstance(payload, dict):
            return payload
        name = payload.get("name", "?")
        content = payload.get("content", "")
        is_error = payload.get("is_error", False)
        rid = str(payload.get("request_id", "-"))
        step = payload.get("step", "-")
        _logger.info(
            "tool_metrics | rid=%s step=%s tool=%s result_len=%s is_error=%s",
            rid, step, name, len(content), is_error,
        )
        return payload

    @staticmethod
    def _full_traceback_error_hook(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """on_error hook：记录完整堆栈（补充默认 hook 的简短警告）。"""
        if not isinstance(payload, dict):
            return payload
        err = payload.get("error")
        if err is None:
            return payload
        tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
        rid = str(payload.get("request_id", "-"))
        step = payload.get("step", "-")
        _logger.error(
            "full_traceback | rid=%s step=%s\n%s",
            rid, step, tb,
        )
        return payload

    def register(self, hook_name: str, func: HookFunc) -> None:
        name = str(hook_name or "").strip()
        if name not in SUPPORTED_HOOKS:
            raise ValueError(f"unsupported hook: {hook_name!r}")
        with self._lock:
            self._hooks[name].append(func)

    def clear(self, hook_name: Optional[str] = None) -> None:
        with self._lock:
            if hook_name is None:
                for name in self._hooks:
                    self._hooks[name].clear()
            elif hook_name in SUPPORTED_HOOKS:
                self._hooks[hook_name].clear()

    def emit(
        self,
        hook_name: str,
        payload: Dict[str, Any],
        *,
        logger=None,
    ) -> Dict[str, Any]:
        name = str(hook_name or "").strip()
        if name not in SUPPORTED_HOOKS:
            return payload
        with self._lock:
            callbacks = list(self._hooks.get(name, []))
        current = payload
        for fn in callbacks:
            try:
                result = fn(current)
                if isinstance(result, dict):
                    current = result
            except Exception as exc:
                if logger is not None:
                    logger.warning(
                        "hook failed | hook=%s func=%s err=%s",
                        name, getattr(fn, "__name__", fn.__class__.__name__), str(exc),
                    )
        return current


def build_default_hook_manager() -> HookManager:
    """
    构建带标准 hook 的 HookManager：
    - on_error：简短警告（内置）+ 完整堆栈
    - after_tool：工具调用指标（名称、结果长度、是否出错）
    """
    hm = HookManager(enable_default_error_hook=True)
    hm.register(HOOK_ON_ERROR, HookManager._full_traceback_error_hook)
    hm.register(HOOK_AFTER_TOOL, HookManager._tool_metrics_hook)
    return hm
