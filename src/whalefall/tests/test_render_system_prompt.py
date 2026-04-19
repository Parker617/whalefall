"""
系统提示词装配（render_system_prompt）回归测试。

覆盖点：
  - 默认 general agent 装配后含 BASE_IDENTITY / ENV_INFO / GUARDRAILS / TOOL 块
  - `custom_base` 参数整体替换 BASE_IDENTITY 并跳过 ENV_INFO
  - render_system_prompt 不会读取任何文件（零文件嗅探不变量）
  - 子 agent（explore/plan/verify/echo-tester）默认不含 BASE_IDENTITY 之外
    的额外身份开场（它们各自的 system_prompt 由 AGENT.md body 决定）
"""
from __future__ import annotations

import pytest

from whalefall.agent.roles import (
    PromptPart,
    get_agent,
    load_agents,
    render_system_prompt,
    render_system_prompt_split,
)


@pytest.fixture(scope="module", autouse=True)
def _loaded_agents() -> None:
    load_agents()


def test_general_default_assembly_contains_core_blocks() -> None:
    agent = get_agent("general")
    out = render_system_prompt(agent, registry=None)
    assert "你是专业的 AI 助手" in out                # BASE_IDENTITY
    assert "当前环境信息" in out                        # ENV_INFO
    assert "[诚实与执行约束]" in out                    # GUARDRAILS


def test_custom_base_replaces_identity_and_skips_env_info() -> None:
    agent = get_agent("general")
    out = render_system_prompt(
        agent, registry=None, custom_base="CUSTOM_BASE_MARK"
    )
    assert out.startswith("CUSTOM_BASE_MARK")
    assert "你是专业的 AI 助手" not in out
    assert "当前环境信息" not in out


def test_env_info_at_tail_for_prompt_cache_friendly_ordering() -> None:
    """ENV_INFO 含动态日期，应排在 BASE_IDENTITY / GUARDRAILS 等静态块之后。"""
    agent = get_agent("general")
    out = render_system_prompt(agent, registry=None)
    idx_base = out.find("你是专业的 AI 助手")
    idx_guard = out.find("[诚实与执行约束]")
    idx_env = out.find("当前环境信息")
    assert idx_base >= 0 and idx_guard >= 0 and idx_env >= 0
    assert idx_base < idx_guard < idx_env


def test_render_system_prompt_does_not_read_any_file(monkeypatch) -> None:
    """零嗅探回归：render_system_prompt 内部绝不做磁盘 I/O。
    允许 load_agents() 在启动阶段读 AGENT.md（这是定义装载，不是嗅探）；
    但一旦 AgentConfig 就绪，渲染 system prompt 不允许再碰磁盘。
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
    assert "当前环境信息" not in s1, "ENV_INFO 属于动态段，不能进静态前缀"


def test_split_merge_matches_render_system_prompt() -> None:
    """split 版本拼接后应与 legacy render_system_prompt 完全一致。"""
    agent = get_agent("general")
    static_part, dyn_part = render_system_prompt_split(agent, registry=None)
    merged = static_part + ("\n\n" + dyn_part if dyn_part else "")
    full = render_system_prompt(agent, registry=None)
    assert merged == full
