"""
Agent 事件流：run_stream() 生成的事件类型。

独立于 agent.roles（定义）与 agent.loop（运行时），避免循环依赖。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union


@dataclass
class TextDeltaEvent:
    """LLM 输出文本片段（streaming delta）。"""
    text: str


@dataclass
class ToolStartEvent:
    """工具开始执行。"""
    name: str
    args: Dict[str, Any]
    step: int


@dataclass
class ToolEndEvent:
    """工具执行完成。"""
    name: str
    content: str
    elapsed: float
    is_error: bool
    step: int


@dataclass
class CompactionEvent:
    """Context 已压缩。"""
    before_tokens: int
    after_tokens: int


@dataclass
class DoneEvent:
    """Agent 完成（final text）。"""
    text: str
    steps: int
    session_messages: Optional[List[Dict[str, Any]]] = None


AgentEvent = Union[
    TextDeltaEvent, ToolStartEvent, ToolEndEvent, CompactionEvent, DoneEvent
]


__all__ = [
    "TextDeltaEvent",
    "ToolStartEvent",
    "ToolEndEvent",
    "CompactionEvent",
    "DoneEvent",
    "AgentEvent",
]
