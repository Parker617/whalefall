"""
AgentConfig：Agent 运行配置数据类 + 工具名归一化/写工具判定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from whalefall.agent.roles.parts import PromptPart


# 静态写工具名单（PermissionManager 启动阶段的保底集合）。
#
# 注意：这只是 ToolRegistry 还未构建时的"启动保底"。
# 运行时真实的写/读属性来自每个 `BuiltinTool.read_only` 字段，由
# `ToolRegistry.is_write_tool_by_name()` 查询；MCP 工具则走 `MCPClient.is_destructive()`。
# 请保持此集合只列**内建工具真实存在的名字**，避免误信。
WRITE_TOOLS: frozenset[str] = frozenset({
    "bash",
    "write",
    "edit",
    "notebook_edit",
    "task_create",
    "task_update",
})


def normalize_tool_name(tool_name: str) -> str:
    """归一化工具名，兼容 mcp__server__tool、server__tool、stdio:server:tool 等格式。"""
    name = (tool_name or "").strip()
    if not name:
        return ""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    if "__" in name:
        return name.rsplit("__", 1)[-1]
    if ":" in name:
        return name.rsplit(":", 1)[-1]
    return name


def is_write_tool(tool_name: str) -> bool:
    """判断给定名称的工具是否属于写工具（含格式归一化后的判定）。"""
    base = normalize_tool_name(tool_name)
    return tool_name in WRITE_TOOLS or base in WRITE_TOOLS


# 缺省 include：覆盖绝大多数"通用主 Agent"的场景
# 注：具体工具使用规范下沉到各 BuiltinTool.prompt()，由 TOOL_REFERENCES 自动汇总。
DEFAULT_INCLUDE: tuple[PromptPart, ...] = (
    PromptPart.BASE_IDENTITY,
    PromptPart.ENV_INFO,
    PromptPart.PROJECT_PROMPT,
    PromptPart.SYSTEM_PROMPT,
    PromptPart.GUARDRAILS,
    PromptPart.TOOL_REFERENCES,
)


@dataclass
class AgentConfig:
    """
    Agent 定义：身份 + 权限 + 系统提示词装配规则。

    身份／描述：
      - name           : 唯一标识，也是 AgentTool 的 subagent_type
      - description    : 简短描述（日志/UI 展示用）
      - model          : 可选，覆盖默认模型

    运行时权限（统一三态：None=全开 / []=全禁 / [...]=白名单）：
      - max_turns             : 循环最大轮数
      - allow_write_tools     : 是否允许写工具（False 则 bash/write/edit 等被过滤）
      - allow_subagent        : 是否允许调用子 Agent（AgentTool）
      - allowed_mcp_servers   : 可见的 MCP server 列表
          - None = 全部 MCP server 可见（默认）
          - []   = 完全禁用 MCP 工具
          - [x, y] = 仅这两个 server 可见
      - allowed_skill_paths   : 可见 skill 路径前缀列表
          - None = 全部 skill 可见（默认）
          - []   = 完全禁用 skill
          - [...] = 路径前缀白名单；以 "/" 结尾为目录前缀（含嵌套），不以 "/" 结尾为精确 skill 名

    系统提示词装配：
      - system_prompt  : Agent 独有的系统提示词正文（取自 definitions/<name>/AGENT.md body）
      - include        : PromptPart 顺序列表，控制最终 system prompt 由哪些积木拼成
                         （注意 PromptPart.PROJECT_PROMPT 的数据源是运行时显式传入的
                          project_prompt 参数，不会从任何文件系统位置自动读取）
    """
    name: str
    description: str = ""
    model: Optional[str] = None
    max_turns: int = 100

    allow_write_tools: bool = True
    allow_subagent: bool = True
    allowed_mcp_servers: Optional[List[str]] = None
    allowed_skill_paths: Optional[List[str]] = None

    system_prompt: str = ""
    include: List[PromptPart] = field(
        default_factory=lambda: list(DEFAULT_INCLUDE)
    )


__all__ = [
    "WRITE_TOOLS",
    "normalize_tool_name",
    "is_write_tool",
    "DEFAULT_INCLUDE",
    "AgentConfig",
]
