"""
BuiltinTool 抽象基类 + ToolContext/ToolResult 数据类。
"""
from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolContext:
    """
    工具执行时的共享上下文（整个 Agent session 共享一个实例）。
    工具通过此上下文访问注册表、MCP 客户端、LLM 等资源。

    agent_name: 当前运行的 Agent 名字（对应 AgentConfig.name），供工具按需调整行为。
    """
    agent_name: str = ""
    tool_registry: Any = None
    mcp_client: Any = None
    llm_client: Any = None
    permission_manager: Any = None
    hook_manager: Any = None
    abort_event: Optional[asyncio.Event] = None
    # ReadTool 记录最近读取的文件，context 压缩后用于恢复。
    # 由于 skill 目录统一用 `read` 工具按 SKILL.md 路径加载，读过的 skill 正文也会被
    # 记入此列表，压缩后由同一条 reminder 恢复，不需要单独的 skill 通道。
    recently_read: List[Dict[str, Any]] = field(default_factory=list)
    # TaskStore 通过 metadata["_task_store"] 懒加载（增量 CRUD + 持久化）
    # AskUserQuestionTool 回调：(question: str) -> str
    # None 时回退到 input()（CLI 模式）
    ask_user_callback: Any = None
    # 规划模式标志（EnterPlanModeTool 设置，影响 AgentLoop 行为）
    plan_mode: bool = False
    # 通用扩展字段（其他工具间状态共享）
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """工具执行结果统一格式。"""
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


class BuiltinTool(abc.ABC):
    """
    内建工具抽象基类。子类需覆盖：
      name, description, parameters_schema, execute()

    max_result_chars: 结果字符上限（0 = 不限制，由工具自己处理）。
      AgentLoop 按此截断工具结果，防止超长内容撑爆 context。
    read_only: True = 可并发，False = 写工具串行执行。
    """

    name: str = ""
    description: str = ""
    read_only: bool = True
    max_result_chars: int = 20_000   # 默认 20K；各工具可覆盖
    parameters_schema: Dict[str, Any] = {"type": "object", "properties": {}}

    @abc.abstractmethod
    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        """
        执行工具逻辑。返回结果字符串，不要 raise 异常
        （ToolExecutor 会捕获并包装为 is_error=True）。
        """
        ...

    def prompt(self) -> str:
        """
        工具的系统提示词补充（可选覆盖）。
        返回非空字符串时会被注入系统提示词的工具指引区块，
        用于提供比 description 更详细的使用说明或约束。
        """
        return ""

    def to_openai_schema(self, agent_config: Any = None) -> Dict[str, Any]:
        """
        返回 OpenAI function 格式的工具 schema。

        agent_config: 保留入参兼容 agent-aware 工具未来扩展（如按 agent 过滤参数枚举），
        默认实现忽略此参数。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} read_only={self.read_only}>"
