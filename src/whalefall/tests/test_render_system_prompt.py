"""
Layer 3 项目提示词 (project_prompt) 装配测试。

覆盖点：
  - PromptPart.PROJECT_PROMPT 被 include 时，None/空串 → 该层整段跳过
  - 非空字符串 → 渲染为 `[项目提示词]` 块并插入在 Layer 2 之后、Layer 4 之前
  - 未 include PROJECT_PROMPT 的子 agent 即使传入 project_prompt 也不会出现在输出
  - load_project_prompt_from_file 支持 @include 递归展开
  - render_system_prompt 不会自动读取任何文件系统路径（嗅探已彻底删除）
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

import pytest

from whalefall.agent.roles import (
    AgentConfig,
    PromptPart,
    get_agent,
    load_agents,
    load_project_prompt_from_file,
    render_project_prompt,
    render_system_prompt,
)


@pytest.fixture(scope="module", autouse=True)
def _loaded_agents() -> None:
    load_agents()


# ────────────────────── render_project_prompt ────────────────────── #

@pytest.mark.parametrize("val", [None, "", "   ", "\n\t\n"])
def test_render_project_prompt_empty(val: object) -> None:
    assert render_project_prompt(val) == ""  # type: ignore[arg-type]


def test_render_project_prompt_non_empty_trim() -> None:
    out = render_project_prompt("  # proj\nrule1\n  ")
    assert out == "[项目提示词]\n# proj\nrule1"


# ────────────────────── render_system_prompt Layer 3 ────────────────────── #

def _find_project_block(text: str) -> str:
    idx = text.find("[项目提示词]")
    return text[idx:] if idx >= 0 else ""


def test_general_project_prompt_visible_when_provided() -> None:
    agent = get_agent("general")
    out = render_system_prompt(agent, registry=None, project_prompt="# GENERAL_MARKER\nxxx")
    assert "[项目提示词]" in out
    assert "GENERAL_MARKER" in out


def test_general_project_prompt_absent_when_none() -> None:
    agent = get_agent("general")
    out = render_system_prompt(agent, registry=None, project_prompt=None)
    assert "[项目提示词]" not in out
    assert "AGENT.md" not in out, "zero-sniff check: no AGENT.md reference should leak"


def test_general_project_prompt_absent_when_blank() -> None:
    agent = get_agent("general")
    out = render_system_prompt(agent, registry=None, project_prompt="   \n\n")
    assert "[项目提示词]" not in out


def test_child_agents_without_project_prompt_include_are_unaffected() -> None:
    """explore / plan / verify / echo-tester 的 include 不含 PROJECT_PROMPT。"""
    for name in ("explore", "plan", "verify", "echo-tester"):
        agent = get_agent(name)
        assert PromptPart.PROJECT_PROMPT not in agent.include, (
            f"{name} unexpectedly includes PROJECT_PROMPT; "
            "update this test if the design changes"
        )
        out = render_system_prompt(agent, registry=None, project_prompt="LEAKED")
        assert "LEAKED" not in out, f"{name} leaked project_prompt despite no include"
        assert "[项目提示词]" not in out


def test_layer_order_layer3_between_layer2_and_layer4() -> None:
    """验证 project_prompt 出现在 env_info 之后、agent 自身 system_prompt 之前。"""
    agent = get_agent("general")
    out = render_system_prompt(
        agent, registry=None, project_prompt="# ORDER_MARK"
    )
    idx_env = out.find("当前环境信息")
    idx_proj = out.find("[项目提示词]")
    idx_guard = out.find("[诚实与执行约束]")
    assert idx_env >= 0 and idx_proj >= 0 and idx_guard >= 0
    assert idx_env < idx_proj < idx_guard


def test_custom_base_skips_env_info() -> None:
    agent = get_agent("general")
    out = render_system_prompt(
        agent, registry=None, custom_base="CUSTOM_BASE_MARK", project_prompt="pp"
    )
    assert out.startswith("CUSTOM_BASE_MARK")
    assert "当前环境信息" not in out
    assert "[项目提示词]\npp" in out


# ────────────────────── load_project_prompt_from_file ────────────────────── #

def test_load_project_prompt_from_file_plain(tmp_path: Path) -> None:
    p = tmp_path / "pp.md"
    p.write_text("hello-from-file", encoding="utf-8")
    assert load_project_prompt_from_file(p) == "hello-from-file"


def test_load_project_prompt_from_file_with_include(tmp_path: Path) -> None:
    part = tmp_path / "part.md"
    part.write_text("INCLUDED_CONTENT", encoding="utf-8")
    main = tmp_path / "main.md"
    main.write_text("top\n@include part.md\nbottom", encoding="utf-8")
    out = load_project_prompt_from_file(main)
    assert "top" in out and "INCLUDED_CONTENT" in out and "bottom" in out


def test_load_project_prompt_from_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_project_prompt_from_file(tmp_path / "nope.md") == ""


def test_render_system_prompt_does_not_read_any_file(monkeypatch, tmp_path: Path) -> None:
    """
    零嗅探回归：render_system_prompt 内部不应调用 Path.read_text / open。
    防止历史上的 "cwd/AGENT.md 自动读" 复活。
    """
    from whalefall.agent.roles import parts as parts_mod

    def _boom_read_md(*a, **kw):  # noqa: ARG001
        raise AssertionError("render_system_prompt leaked file I/O via _read_md_file")

    monkeypatch.setattr(parts_mod, "_read_md_file", _boom_read_md)
    out = render_system_prompt(
        get_agent("general"), registry=None, project_prompt="# OK"
    )
    assert "# OK" in out
