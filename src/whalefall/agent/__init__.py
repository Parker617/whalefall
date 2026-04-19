"""
Agent 模块：类型 / 配置 / 事件 的聚合导出。

分层：
  - agent.roles  ：Agent 定义（AgentConfig / PromptPart / 加载器）
  - agent.events ：事件流 dataclass（TextDelta / ToolStart / ToolEnd / Compaction / Done）
  - agent.loop / executor / query_engine / compaction / hooks ：运行时
  - 持久化相关（session_store / trace / retention）已迁至 whalefall.storage

重组件请直接从子模块导入：
  from whalefall.agent.loop            import AgentLoop
  from whalefall.agent.query_engine    import QueryEngine
  from whalefall.agent.compaction      import ContextManager
  from whalefall.agent.executor        import ToolExecutor
  from whalefall.agent.hooks           import HookManager
  from whalefall.storage.session_store import SessionStore
"""
from whalefall.agent.events import (
    AgentEvent,
    CompactionEvent,
    DoneEvent,
    TextDeltaEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from whalefall.agent.roles import (
    AgentConfig,
    PromptPart,
    WRITE_TOOLS,
    get_agent,
    is_write_tool,
    list_agent_names,
    load_agents,
    normalize_tool_name,
    render_system_prompt,
)

__all__ = [
    # 定义
    "AgentConfig",
    "PromptPart",
    "WRITE_TOOLS",
    "is_write_tool",
    "normalize_tool_name",
    "load_agents",
    "list_agent_names",
    "get_agent",
    "render_system_prompt",
    # 事件
    "AgentEvent",
    "TextDeltaEvent",
    "ToolStartEvent",
    "ToolEndEvent",
    "CompactionEvent",
    "DoneEvent",
]
