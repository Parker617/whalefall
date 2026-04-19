"""
Agent 定义加载器。

职责：
- 扫描 agent/roles/definitions/<name>/AGENT.md
- 解析 YAML frontmatter + body，得到 AgentConfig
- 装配最终 system prompt（按 AgentConfig.include 驱动）

内建 agent（general/explore/plan/verify）和用户自定义 agent 共用同一套格式，
统一通过 load_agents() 返回。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from whalefall.agent.roles.config import AgentConfig, DEFAULT_INCLUDE
from whalefall.agent.roles.parts import (
    BASE_IDENTITY,
    BEHAVIOR_GUARDRAILS,
    TONE_STYLE,
    PromptPart,
    collect_mcp_instructions,
    collect_tool_references,
    render_env_info,
)
from whalefall.core.log import get_logger

logger = get_logger("whalefall.agent.roles")

_DEFINITIONS_DIR = Path(__file__).resolve().parent / "definitions"

_FRONTMATTER_RE = re.compile(
    r"^\s*---\s*\n(?P<meta>.*?)\n---\s*\n?(?P<body>.*)$",
    re.DOTALL,
)


def _parse_frontmatter(text: str) -> tuple[Dict[str, str], str]:
    """轻量 YAML frontmatter 解析（只处理 k:v 与列表字符串）。"""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text.strip()
    meta: Dict[str, str] = {}
    for line in m.group("meta").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip().lower()] = v.strip().strip("\"'")
    return meta, m.group("body").strip()


def _parse_bool(value: str, default: bool) -> bool:
    if value == "":
        return default
    return value.lower() in ("true", "1", "yes", "on")


def _parse_list(value: str) -> List[str]:
    if not value:
        return []
    v = value.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return [x.strip().strip("\"'") for x in v.split(",") if x.strip()]


def _parse_include(value: str) -> List[PromptPart]:
    names = _parse_list(value)
    out: List[PromptPart] = []
    for n in names:
        try:
            out.append(PromptPart(n.lower()))
        except ValueError:
            logger.warning("unknown PromptPart '%s' ignored", n)
    return out


def _load_one(agent_md: Path) -> Optional[AgentConfig]:
    try:
        text = agent_md.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("read agent md failed | path=%s err=%s", agent_md, exc)
        return None

    meta, body = _parse_frontmatter(text)
    name = meta.get("name") or agent_md.parent.name
    if not name:
        logger.warning("agent md missing name | path=%s", agent_md)
        return None

    try:
        max_turns = int(meta.get("max_turns", "100"))
    except ValueError:
        max_turns = 100

    include_raw = meta.get("include", "")
    include = _parse_include(include_raw) if include_raw else list(DEFAULT_INCLUDE)

    # 三态字段：区分"未设置"（None，全开）与"空列表"（[]，全禁）
    # frontmatter 里 key 不写 → 不进 meta → None
    # 写了 key: [] → 进 meta 且 value="[]" → 解析为 []
    def _three_state(key: str) -> Optional[List[str]]:
        if key not in meta:
            return None
        return _parse_list(meta[key])

    return AgentConfig(
        name=name,
        description=meta.get("description", ""),
        model=meta.get("model") or None,
        max_turns=max_turns,
        allow_write_tools=_parse_bool(meta.get("allow_write_tools", ""), True),
        allow_subagent=_parse_bool(meta.get("allow_subagent", ""), True),
        allowed_mcp_servers=_three_state("allowed_mcp_servers"),
        allowed_skill_paths=_three_state("allowed_skill_paths"),
        system_prompt=body,
        include=include,
    )


def load_agents() -> Dict[str, AgentConfig]:
    """扫描 agent/roles/definitions/ 下所有 AGENT.md，返回 {name: AgentConfig}。"""
    agents: Dict[str, AgentConfig] = {}
    if not _DEFINITIONS_DIR.is_dir():
        logger.warning("definitions dir missing | path=%s", _DEFINITIONS_DIR)
        return agents
    for agent_md in sorted(_DEFINITIONS_DIR.rglob("AGENT.md")):
        cfg = _load_one(agent_md)
        if cfg is None:
            continue
        if cfg.name in agents:
            logger.warning("duplicate agent name, overwriting | name=%s", cfg.name)
        agents[cfg.name] = cfg
    return agents


def list_agent_names() -> List[str]:
    """列出当前目录下所有 agent 名称。"""
    return sorted(load_agents().keys())


def get_agent(name: str) -> AgentConfig:
    """按名取 agent。找不到回退到 general；general 也不存在则抛错。"""
    agents = load_agents()
    if name in agents:
        return agents[name]
    general = agents.get("general")
    if general is None:
        raise RuntimeError(
            "Default agent 'general' not found under agent/roles/definitions/"
        )
    logger.info("agent '%s' not found, falling back to 'general'", name)
    return general


def render_system_prompt_split(
    agent: AgentConfig,
    *,
    registry: Any = None,
    custom_base: Optional[str] = None,
    mcp_client: Any = None,
    model: Optional[str] = None,
) -> tuple[str, str]:
    """
    把 system prompt 拆成 (static_prefix, dynamic_suffix)。

    - **static_prefix**：每次装配字节稳定的部分（身份 / agent body / 守则 /
      风格 / 工具使用指引 / MCP server 使用说明）。LLM provider 的 prompt cache
      可以把它当成长前缀命中。
    - **dynamic_suffix**：每次装配都会变（ENV_INFO 带日期时间、git/model 等）。
      放在最后，不会破坏前缀缓存。

    顺序遵循 `agent.include`；如果 ENV_INFO 被声明，它会被隔离到 dynamic 段。
    `custom_base` 非空时整体替换 BASE_IDENTITY，并强制跳过 ENV_INFO（节点型
    调用方自己控制上下文，不需要框架再注入环境信息）。
    """
    static_parts: List[str] = []
    dynamic_parts: List[str] = []
    seen_base = False

    for part in agent.include:
        if part == PromptPart.BASE_IDENTITY:
            static_parts.append(custom_base if custom_base else BASE_IDENTITY)
            seen_base = True
        elif part == PromptPart.ENV_INFO:
            if not (seen_base and custom_base):
                dynamic_parts.append(render_env_info(model=model))
        elif part == PromptPart.SYSTEM_PROMPT:
            if agent.system_prompt:
                static_parts.append(agent.system_prompt)
        elif part == PromptPart.GUARDRAILS:
            static_parts.append(BEHAVIOR_GUARDRAILS)
        elif part == PromptPart.TONE_STYLE:
            static_parts.append(TONE_STYLE)
        elif part == PromptPart.TOOL_REFERENCES:
            tr = collect_tool_references(registry, agent_config=agent)
            if tr:
                static_parts.append(tr)
        elif part == PromptPart.MCP_INSTRUCTIONS:
            mi = collect_mcp_instructions(
                mcp_client, allowed_servers=agent.allowed_mcp_servers
            )
            if mi:
                static_parts.append(mi)

    static_prefix = "\n\n".join(p for p in static_parts if p)
    dynamic_suffix = "\n\n".join(p for p in dynamic_parts if p)
    return static_prefix, dynamic_suffix


def render_system_prompt(
    agent: AgentConfig,
    *,
    registry: Any = None,
    custom_base: Optional[str] = None,
    mcp_client: Any = None,
    model: Optional[str] = None,
) -> str:
    """
    按 agent.include 声明的顺序装配最终 system prompt（合并静态 + 动态段）。

    积木语义：
      BASE_IDENTITY     → 通用身份文本；若 custom_base 传入则用它整体替代（此时同时跳过 ENV_INFO）
      SYSTEM_PROMPT     → agent.system_prompt（来自 definitions/<name>/AGENT.md body）
      GUARDRAILS        → 诚实约束 + 行动风险分级 + <system-reminder> 约定
      TONE_STYLE        → 输出风格与引用格式（path:line / no emoji / no colon 前置）
      TOOL_REFERENCES   → 内建工具的 prompt() 汇总
      MCP_INSTRUCTIONS  → 已连接 MCP server 的 instructions 聚合（按 allowed_mcp_servers 过滤）
      ENV_INFO          → 当前环境信息（日期/cwd/git/shell/platform/model）——动态块，
                          被 `render_system_prompt_split` 隔离在末尾，利于 provider 端
                          prompt cache 命中静态前缀。

    设计说明：
      要针对某个任务/节点**整体替换身份**（如量化分析节点），用
      `AgentLoop.run_*(system_prompt=...)` 传入完整 markdown；该参数对应
      `custom_base` 形参，会替换 BASE_IDENTITY 并自动跳过 ENV_INFO。
    """
    static_prefix, dynamic_suffix = render_system_prompt_split(
        agent,
        registry=registry,
        custom_base=custom_base,
        mcp_client=mcp_client,
        model=model,
    )
    if static_prefix and dynamic_suffix:
        return static_prefix + "\n\n" + dynamic_suffix
    return static_prefix or dynamic_suffix


__all__ = [
    "load_agents",
    "list_agent_names",
    "get_agent",
    "render_system_prompt",
    "render_system_prompt_split",
]
