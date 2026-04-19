"""
agent.roles：Agent 定义与加载子包。

运行时（loop/executor/hooks/compaction/query_engine/session_store）与
Agent 定义（config/parts/loader/definitions）分离：
  - 改"怎么跑"进 agent/
  - 改"什么样"进 agent/roles/

对外暴露：
  - AgentConfig        ：Agent 配置数据类
  - PromptPart         ：可选框架提示词积木
  - load_agents()      ：加载所有定义（内建 + 用户）
  - get_agent(name)    ：按名称取，找不到回退 general
  - render_system_prompt(agent, *, registry) ：按 include 装配最终 system prompt
"""
from whalefall.agent.roles.config import (
    AgentConfig,
    WRITE_TOOLS,
    is_write_tool,
    normalize_tool_name,
)
from whalefall.agent.roles.parts import (
    PromptPart,
    BASE_IDENTITY,
    BEHAVIOR_GUARDRAILS,
    TONE_STYLE,
    SYSTEM_REMINDER_OPEN,
    SYSTEM_REMINDER_CLOSE,
    wrap_system_reminder,
    render_env_info,
    collect_tool_references,
    collect_mcp_instructions,
)
from whalefall.agent.roles.loader import (
    load_agents,
    get_agent,
    render_system_prompt,
    render_system_prompt_split,
    list_agent_names,
)

__all__ = [
    "AgentConfig",
    "WRITE_TOOLS",
    "is_write_tool",
    "normalize_tool_name",
    "PromptPart",
    "BASE_IDENTITY",
    "BEHAVIOR_GUARDRAILS",
    "TONE_STYLE",
    "SYSTEM_REMINDER_OPEN",
    "SYSTEM_REMINDER_CLOSE",
    "wrap_system_reminder",
    "render_env_info",
    "collect_tool_references",
    "collect_mcp_instructions",
    "load_agents",
    "get_agent",
    "render_system_prompt",
    "render_system_prompt_split",
    "list_agent_names",
]
