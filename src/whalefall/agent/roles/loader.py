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
    PromptPart,
    collect_tool_references,
    load_project_agent_md,
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


def render_system_prompt(
    agent: AgentConfig,
    *,
    registry: Any = None,
    custom_base: Optional[str] = None,
) -> str:
    """
    按 agent.include 声明的顺序装配最终 system prompt。

    积木语义：
      BASE_IDENTITY    → 通用身份文本；若 custom_base 传入则用它替代（此时同时跳过 ENV_INFO）
      ENV_INFO         → 当前环境信息（日期/cwd/平台）
      AGENT_MD         → cwd/AGENT.md 项目配置（由 `/init` 生成）
      SYSTEM_PROMPT    → agent.system_prompt（AGENT.md body）
      GUARDRAILS       → 通用诚实约束 + 写操作前置检查
      TOOL_REFERENCES  → 内建工具的 prompt() 汇总（具体工具使用规范下沉到这里）
    """
    parts: List[str] = []
    seen_base = False

    for part in agent.include:
        if part == PromptPart.BASE_IDENTITY:
            parts.append(custom_base if custom_base else BASE_IDENTITY)
            seen_base = True
        elif part == PromptPart.ENV_INFO:
            # custom_base 已包含调用方自己的开场，跳过环境信息避免重复
            if not (seen_base and custom_base):
                parts.append(render_env_info())
        elif part == PromptPart.AGENT_MD:
            md = load_project_agent_md()
            if md:
                parts.append(md)
        elif part == PromptPart.SYSTEM_PROMPT:
            if agent.system_prompt:
                parts.append(agent.system_prompt)
        elif part == PromptPart.GUARDRAILS:
            parts.append(BEHAVIOR_GUARDRAILS)
        elif part == PromptPart.TOOL_REFERENCES:
            tr = collect_tool_references(registry)
            if tr:
                parts.append(tr)

    return "\n\n".join(p for p in parts if p)


__all__ = [
    "load_agents",
    "list_agent_names",
    "get_agent",
    "render_system_prompt",
]
