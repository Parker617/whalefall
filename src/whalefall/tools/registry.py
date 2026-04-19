"""
ToolRegistry：内建工具注册表（仅管内建工具，不存 MCP schema）。

- register_builtin()：注册内建工具
- schemas(agent_config=...)：按 AgentConfig 过滤返回内建工具 OpenAI schema 列表
  - allow_write_tools=False 的 agent 只会看到 read_only=True 的工具
  - 其它 agent 看到全部内建工具
- is_builtin(name)：工具类型判断
- get_builtin(name)：获取内建工具实例

MCP 工具由 AgentLoop._get_tools() 通过 mcp_client.list_tools() 获取并汇合。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

from whalefall.core.log import get_logger

if TYPE_CHECKING:
    from whalefall.agent.roles import AgentConfig
    from whalefall.tools.base import BuiltinTool

logger = get_logger("whalefall.registry")


class ToolRegistry:
    """
    内建工具注册表：仅管理进程内直接执行的内建工具。

    工具名规范：
    - 内建工具：bash / read / write / edit / glob / grep / web_fetch / agent 等

    MCP 工具不在此注册表中，由 AgentLoop 通过 MCPClient.list_tools() 获取。
    """

    def __init__(self):
        self._builtins: Dict[str, "BuiltinTool"] = {}           # name -> BuiltinTool 实例
        self._logger = logger

    # ------------------------------------------------------------------ #
    #                       注册方法                                       #
    # ------------------------------------------------------------------ #
    def register_builtin(self, tool: "BuiltinTool") -> None:
        """注册内建工具。"""
        if not tool.name:
            raise ValueError(f"BuiltinTool 必须有 name 属性: {tool}")
        if tool.name in self._builtins:
            self._logger.warning("overwriting builtin tool | name=%s", tool.name)
        self._builtins[tool.name] = tool
        self._logger.info("registered builtin | name=%s read_only=%s", tool.name, tool.read_only)

    # ------------------------------------------------------------------ #
    #                       查询方法                                       #
    # ------------------------------------------------------------------ #
    def schemas(
        self,
        *,
        agent_config: Optional["AgentConfig"] = None,
        include_write_tools: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """
        返回内建工具的 OpenAI schema 列表。

        过滤规则：
        - 若传入 agent_config 且 allow_write_tools=False，仅返回 read_only=True 的工具。
        - 若显式传入 include_write_tools=False，等价于上一条。
        - 未指定时默认返回全部内建工具。
        """
        if include_write_tools is None:
            include_write_tools = True
            if agent_config is not None:
                include_write_tools = bool(getattr(agent_config, "allow_write_tools", True))

        result: List[Dict[str, Any]] = []
        for tool in self._builtins.values():
            if not include_write_tools and not getattr(tool, "read_only", True):
                continue
            result.append(tool.to_openai_schema(agent_config=agent_config))
        return result

    def get_builtin(self, name: str) -> Optional["BuiltinTool"]:
        """根据名称获取内建工具实例。"""
        return self._builtins.get(name)

    def is_builtin(self, name: str) -> bool:
        """判断是否为内建工具。"""
        return name in self._builtins

    def is_write_tool_by_name(self, name: str) -> Optional[bool]:
        """
        按注册表中的 `BuiltinTool.read_only` 判断该名称的工具是否为写工具。
        返回 None 表示注册表里没有此工具（由调用方决定是否回退到静态 WRITE_TOOLS）。
        """
        tool = self._builtins.get(name)
        if tool is None:
            return None
        return not bool(getattr(tool, "read_only", True))

    def all_builtin_names(self) -> List[str]:
        """返回所有已注册内建工具名列表。"""
        return list(self._builtins.keys())

    def all_builtins(self) -> List["BuiltinTool"]:
        """返回所有已注册内建工具实例列表。"""
        return list(self._builtins.values())

    def __len__(self) -> int:
        return len(self._builtins)

    def __repr__(self) -> str:
        return f"<ToolRegistry builtins={len(self._builtins)}>"


def build_default_registry(
    include_agent_tool: bool = True,
    include_web_search: bool = True,
    include_web_browser: bool = True,
    include_notebook_edit: bool = True,
    include_todo: bool = True,
    include_ask_user: bool = True,
    include_plan_mode: bool = True,
    include_sleep: bool = True,
    include_config: bool = True,
) -> ToolRegistry:
    """
    构建包含所有内建工具的默认 ToolRegistry。

    Args:
        include_agent_tool:  是否包含 AgentTool（子 agent 派生）
        include_web_search:  是否包含 WebSearchTool（网络搜索）
        include_web_browser: 是否包含 WebBrowserTool（Playwright 浏览器访问）
        include_notebook_edit: 是否包含 NotebookEditTool（.ipynb 结构化编辑）
        include_todo:        是否包含 TodoWriteTool（任务管理）
        include_ask_user:    是否包含 AskUserQuestionTool（向用户提问）

    Returns:
        已注册所有内建工具的 ToolRegistry 实例

    Skill 机制：skill 不是工具——system prompt 里会直接注入 `skills/**/SKILL.md` 目录
    清单（见 `agent.roles.parts.collect_skills_catalog`），LLM 通过 `read` 工具按需
    打开对应 SKILL.md 读正文，与 Claude Code 的 "Agent Skills" 协议一致。
    """
    from whalefall.tools.bash import BashTool
    from whalefall.tools.read import ReadTool
    from whalefall.tools.write import WriteTool
    from whalefall.tools.edit import EditTool
    from whalefall.tools.glob import GlobTool
    from whalefall.tools.grep import GrepTool
    from whalefall.tools.fetch import WebFetchTool

    registry = ToolRegistry()
    registry.register_builtin(BashTool())
    registry.register_builtin(ReadTool())
    registry.register_builtin(WriteTool())
    registry.register_builtin(EditTool())
    registry.register_builtin(GlobTool())
    registry.register_builtin(GrepTool())
    registry.register_builtin(WebFetchTool())

    if include_web_search:
        from whalefall.tools.web_search import WebSearchTool
        registry.register_builtin(WebSearchTool())

    if include_web_browser:
        from whalefall.tools.web_browser import WebBrowserTool
        registry.register_builtin(WebBrowserTool())

    if include_notebook_edit:
        from whalefall.tools.notebook_edit import NotebookEditTool
        registry.register_builtin(NotebookEditTool())

    if include_todo:
        from whalefall.tools.todo import (
            TaskCreateTool, TaskUpdateTool, TaskGetTool, TaskListTool,
        )
        registry.register_builtin(TaskCreateTool())
        registry.register_builtin(TaskUpdateTool())
        registry.register_builtin(TaskGetTool())
        registry.register_builtin(TaskListTool())

    if include_agent_tool:
        from whalefall.tools.subagent import AgentTool
        registry.register_builtin(AgentTool())

    if include_ask_user:
        from whalefall.tools.ask import AskUserQuestionTool
        registry.register_builtin(AskUserQuestionTool())

    if include_plan_mode:
        from whalefall.tools.plan_mode import EnterPlanModeTool, ExitPlanModeTool
        registry.register_builtin(EnterPlanModeTool())
        registry.register_builtin(ExitPlanModeTool())

    if include_sleep:
        from whalefall.tools.sleep import SleepTool
        registry.register_builtin(SleepTool())

    if include_config:
        from whalefall.tools.config import ConfigTool
        registry.register_builtin(ConfigTool())

    return registry
