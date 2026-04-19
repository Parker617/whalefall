"""
系统提示词装配（render_system_prompt）回归测试。

覆盖点：
  - 默认 general agent 装配后含 BASE_IDENTITY / ENV_INFO / GUARDRAILS /
    TONE_STYLE / TOOL 块
  - `custom_base` 参数整体替换 BASE_IDENTITY 并跳过 ENV_INFO
  - render_system_prompt 不会读取任何文件（零文件嗅探不变量）
  - ENV_INFO 输出 <env>...</env> XML 包裹，且被隔离在 dynamic 段（末尾）
  - <system-reminder> 标签约定与 wrap_system_reminder 行为
  - MCP server instructions 聚合（collect_mcp_instructions）
  - 子 agent（explore/plan/verify/echo-tester）默认能装配不抛错
"""
from __future__ import annotations

import pytest

from whalefall.agent.roles import (
    PromptPart,
    collect_mcp_instructions,
    get_agent,
    load_agents,
    render_env_info,
    render_system_prompt,
    render_system_prompt_split,
    wrap_system_reminder,
)


@pytest.fixture(scope="module", autouse=True)
def _loaded_agents() -> None:
    load_agents()


def test_general_default_assembly_contains_core_blocks() -> None:
    agent = get_agent("general")
    out = render_system_prompt(agent, registry=None)
    assert "你是专业的 AI 助手" in out          # BASE_IDENTITY
    assert "<env>" in out and "</env>" in out    # ENV_INFO (XML 包裹)
    assert "[诚实与执行约束]" in out              # GUARDRAILS
    assert "[输出风格与引用格式]" in out          # TONE_STYLE
    assert "[行动风险分级]" in out                # GUARDRAILS blast radius
    assert "<system-reminder>" in out            # GUARDRAILS 中的 system-reminder 说明


def test_custom_base_replaces_identity_and_skips_env_info() -> None:
    agent = get_agent("general")
    out = render_system_prompt(
        agent, registry=None, custom_base="CUSTOM_BASE_MARK"
    )
    assert out.startswith("CUSTOM_BASE_MARK")
    assert "你是专业的 AI 助手" not in out
    assert "<env>" not in out


def test_env_info_at_tail_for_prompt_cache_friendly_ordering() -> None:
    """<env> 块含动态日期，应排在 BASE_IDENTITY / GUARDRAILS 等静态块之后。"""
    agent = get_agent("general")
    out = render_system_prompt(agent, registry=None)
    idx_base = out.find("你是专业的 AI 助手")
    idx_guard = out.find("[诚实与执行约束]")
    idx_env = out.find("<env>")
    assert idx_base >= 0 and idx_guard >= 0 and idx_env >= 0
    assert idx_base < idx_guard < idx_env


def test_env_info_includes_git_shell_platform_keys() -> None:
    out = render_env_info()
    for key in ("Working directory:", "Is directory a git repo:",
                "Platform:", "Shell:", "Python:", "Date:"):
        assert key in out, f"env_info 缺 `{key}` 字段"
    assert out.startswith("<env>") and out.rstrip().endswith("</env>")


def test_env_info_appends_model_line_when_model_given() -> None:
    out = render_env_info(model="qwen-max")
    assert "powered by the model `qwen-max`" in out


def test_render_system_prompt_does_not_read_any_file(monkeypatch) -> None:
    """零嗅探回归：render_system_prompt 内部绝不做磁盘 I/O。
    允许 load_agents() 在启动阶段读 AGENT.md（这是定义装载，不是嗅探）；
    但一旦 AgentConfig 就绪，渲染 system prompt 不允许再碰磁盘。
    注意：env_info 里会通过 subprocess 调用 `git rev-parse` 探测 is-git-repo，
    这不走 Path.read_text，因此与"零文件嗅探"互不冲突。
    """
    import pathlib

    agent = get_agent("general")

    def _boom(*_a, **_kw):
        raise AssertionError("render_system_prompt leaked file I/O")

    monkeypatch.setattr(pathlib.Path, "read_text", _boom)
    out = render_system_prompt(agent, registry=None)
    assert "你是专业的 AI 助手" in out


def test_child_agents_assemble_without_crash() -> None:
    """所有内建子 agent 的 include 列表都能被正常装配（不抛异常）。"""
    for name in ("explore", "plan", "verify"):
        agent = get_agent(name)
        out = render_system_prompt(agent, registry=None)
        assert isinstance(out, str) and out.strip()
        assert PromptPart.BASE_IDENTITY in agent.include or agent.system_prompt


def test_static_prefix_byte_stable_across_calls() -> None:
    """prompt cache 友好性回归：静态前缀跨次调用必须字节一致。"""
    agent = get_agent("general")
    s1, _ = render_system_prompt_split(agent, registry=None)
    s2, _ = render_system_prompt_split(agent, registry=None)
    assert s1 == s2, "静态前缀字节必须稳定，否则 prompt cache 命中失败"
    assert "<env>" not in s1, "ENV_INFO 属于动态段，不能进静态前缀"


def test_split_merge_matches_render_system_prompt() -> None:
    """split 版本拼接后应与 legacy render_system_prompt 完全一致。"""
    agent = get_agent("general")
    static_part, dyn_part = render_system_prompt_split(agent, registry=None)
    merged = static_part + ("\n\n" + dyn_part if dyn_part else "")
    full = render_system_prompt(agent, registry=None)
    assert merged == full


# ── <system-reminder> 约定 ──────────────────────────────────────────────

def test_wrap_system_reminder_basic() -> None:
    out = wrap_system_reminder("hello world", title="t1")
    assert out.startswith("<system-reminder>\n")
    assert out.endswith("\n</system-reminder>")
    assert "t1" in out
    assert "hello world" in out


def test_wrap_system_reminder_empty_returns_empty() -> None:
    assert wrap_system_reminder("") == ""
    assert wrap_system_reminder("   ", title="x") == ""


# ── MCP instructions 聚合 ──────────────────────────────────────────────

class _FakeMCPClient:
    def __init__(self, instructions_map):
        self._m = instructions_map

    def list_instructions(self, servers=None):
        if servers is None:
            return list(self._m.items())
        if not servers:
            return []
        return [(n, t) for n, t in self._m.items() if n in set(servers)]


def test_collect_mcp_instructions_none_client_returns_empty() -> None:
    assert collect_mcp_instructions(None) == ""


def test_collect_mcp_instructions_aggregates_all_by_default() -> None:
    client = _FakeMCPClient({"alpha": "use alpha like this", "beta": "beta rules"})
    out = collect_mcp_instructions(client)
    assert "[MCP Server 使用说明]" in out
    assert "## alpha" in out and "use alpha like this" in out
    assert "## beta" in out and "beta rules" in out


def test_collect_mcp_instructions_whitelist() -> None:
    client = _FakeMCPClient({"alpha": "A", "beta": "B", "gamma": "C"})
    out = collect_mcp_instructions(client, allowed_servers=["alpha", "gamma"])
    assert "## alpha" in out and "## gamma" in out
    assert "## beta" not in out


def test_collect_mcp_instructions_empty_whitelist_returns_empty() -> None:
    client = _FakeMCPClient({"alpha": "A"})
    assert collect_mcp_instructions(client, allowed_servers=[]) == ""


def test_render_system_prompt_with_mcp_instructions_in_static() -> None:
    """MCP 使用说明属于静态块，应出现在 <env> 之前。"""
    agent = get_agent("general")
    client = _FakeMCPClient({"demo": "call demo__echo with text"})
    out = render_system_prompt(agent, registry=None, mcp_client=client)
    assert "[MCP Server 使用说明]" in out
    idx_mcp = out.find("[MCP Server 使用说明]")
    idx_env = out.find("<env>")
    assert 0 <= idx_mcp < idx_env

    static_part, dyn_part = render_system_prompt_split(
        agent, registry=None, mcp_client=client
    )
    assert "[MCP Server 使用说明]" in static_part
    assert "<env>" in dyn_part
