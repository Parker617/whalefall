"""
StreamHandler：流式输出状态管理，作为 callback 接口层。

设计为 callback 接口，可被 CLI 或其他 UI 实现消费。
不依赖任何特定 UI 框架，可独立使用。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ToolEvent:
    """工具执行事件记录。"""
    tool_name: str
    args: Dict[str, Any]
    start_time: float
    end_time: Optional[float] = None
    result_preview: str = ""
    is_error: bool = False
    elapsed: float = 0.0


@dataclass
class CompactionRecord:
    """
    UI 端保存的一次压缩记录。

    与 `whalefall.agent.events.CompactionEvent` 区分：后者是 agent 事件流
    中一次"压缩发生"的即时事件，本类是 UI 侧累积下来做汇总展示的统计结构。
    """
    before_tokens: int
    after_tokens: int
    timestamp: float = field(default_factory=time.time)

    @property
    def saved_tokens(self) -> int:
        return self.before_tokens - self.after_tokens

    @property
    def ratio(self) -> float:
        if self.before_tokens == 0:
            return 0.0
        return 1.0 - self.after_tokens / self.before_tokens


class StreamHandler:
    """
    流式输出状态管理器。

    使用方式：
        handler = StreamHandler(on_print=print)
        # 作为 AgentLoop 的 callback
        agent_loop.run(
            query,
            on_text=handler.on_text_delta,
            on_tool_start=handler.on_tool_start,
            on_tool_end=handler.on_tool_end,
            on_compaction=handler.on_compaction,
        )
        result = handler.get_text()
    """

    def __init__(
        self,
        on_print: Optional[Callable[[str], None]] = None,
        show_tools: bool = True,
        show_compaction: bool = True,
    ):
        """
        Args:
            on_print: 文本输出回调（None 则不输出）
            show_tools: 是否显示工具事件
            show_compaction: 是否显示压缩事件
        """
        self._on_print = on_print
        self._show_tools = show_tools
        self._show_compaction = show_compaction

        # 状态
        self._text_parts: List[str] = []
        self._tool_events: List[ToolEvent] = []
        self._compaction_events: List[CompactionRecord] = []
        self._current_tool: Optional[ToolEvent] = None
        self._step_count: int = 0

    # ------------------------------------------------------------------ #
    #                       回调方法                                       #
    # ------------------------------------------------------------------ #
    def on_text_delta(self, delta: str) -> None:
        """
        处理 LLM 文本 delta（流式文本片段）。
        累积到 _text_parts，同时通过 on_print 输出。
        """
        if delta:
            self._text_parts.append(delta)
            if self._on_print is not None:
                self._on_print(delta)

    def on_tool_start(self, tool_name: str, args: Dict[str, Any]) -> None:
        """工具开始执行事件。"""
        event = ToolEvent(
            tool_name=tool_name,
            args=args,
            start_time=time.time(),
        )
        self._current_tool = event
        self._tool_events.append(event)

        if self._show_tools and self._on_print is not None:
            args_preview = self._format_args(args)
            self._on_print(f"\n[工具] 调用 {tool_name}: {args_preview}")

    def on_tool_end(self, tool_name: str, result: str, elapsed: float) -> None:
        """工具执行结束事件。"""
        # 找到对应的开始事件
        for event in reversed(self._tool_events):
            if event.tool_name == tool_name and event.end_time is None:
                event.end_time = time.time()
                event.elapsed = elapsed
                event.result_preview = (result or "")[:200]
                event.is_error = (result or "").startswith("错误") or (result or "").startswith("Error")
                self._current_tool = None
                break

        if self._show_tools and self._on_print is not None:
            is_error = (result or "").startswith("错误") or (result or "").startswith("Error")
            status = "失败" if is_error else "完成"
            self._on_print(f"[工具] {tool_name} {status} ({elapsed:.1f}s)")

    def on_compaction(self, before_tokens: int, after_tokens: int) -> None:
        """Context 压缩事件。"""
        event = CompactionRecord(
            before_tokens=before_tokens,
            after_tokens=after_tokens,
        )
        self._compaction_events.append(event)

        if self._show_compaction and self._on_print is not None:
            saved = event.saved_tokens
            ratio = event.ratio * 100
            self._on_print(
                f"\n[上下文压缩] {before_tokens:,} tokens → {after_tokens:,} tokens "
                f"(节省 {saved:,} tokens, {ratio:.0f}%)\n"
            )

    # ------------------------------------------------------------------ #
    #                       查询方法                                       #
    # ------------------------------------------------------------------ #
    def get_text(self) -> str:
        """获取完整的累积文本。"""
        return "".join(self._text_parts)

    def get_tool_events(self) -> List[ToolEvent]:
        """获取工具事件记录。"""
        return list(self._tool_events)

    def get_stats(self) -> Dict[str, Any]:
        """获取执行统计信息。"""
        total_elapsed = sum(e.elapsed for e in self._tool_events)
        error_count = sum(1 for e in self._tool_events if e.is_error)
        return {
            "tool_calls": len(self._tool_events),
            "tool_errors": error_count,
            "total_tool_time": total_elapsed,
            "text_chars": len(self.get_text()),
            "compaction_count": len(self._compaction_events),
        }

    def reset(self) -> None:
        """重置所有状态（用于新一轮对话）。"""
        self._text_parts.clear()
        self._tool_events.clear()
        self._compaction_events.clear()
        self._current_tool = None
        self._step_count = 0

    # ------------------------------------------------------------------ #
    #                       格式化工具方法                                 #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _format_args(args: Dict[str, Any], max_len: int = 80) -> str:
        """格式化工具参数为简短预览。"""
        if not args:
            return "(无参数)"
        try:
            import json
            s = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
            if len(s) > max_len:
                # 找最重要的参数（command / file_path / url / prompt / pattern）
                key_priority = ["command", "file_path", "url", "prompt", "pattern", "query"]
                for key in key_priority:
                    if key in args:
                        val = str(args[key])
                        if len(val) > max_len - len(key) - 3:
                            val = val[:max_len - len(key) - 6] + "..."
                        return f"{key}={val!r}"
                return s[:max_len] + "..."
            return s
        except Exception:
            return str(args)[:max_len]
