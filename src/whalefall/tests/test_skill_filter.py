"""
SkillTool 的 agent-aware 前缀过滤回归测试。

覆盖：
  - _is_skill_allowed 前缀匹配语义（None/[]/尾/无尾/精确）
  - _scan_skills 按 allowed_paths 过滤
  - catalog_lines 同步过滤（给 LLM 看的目录视图）
  - execute 运行期双端校验（防绕过 list 直接 load 被禁的）
  - to_openai_schema 按 agent_config 过滤 description
  - 搭配真实 agent 定义：general / explore 等默认看到全部
"""
from __future__ import annotations

import pytest

from whalefall.agent.roles import load_agents
from whalefall.tools.base import ToolContext
from whalefall.tools.skill import SkillTool


# ── _is_skill_allowed 语义（纯字符串逻辑，不依赖文件系统）────────────────


@pytest.mark.parametrize(
    "rel_name, allowed, expected",
    [
        # None = 全看
        ("general/weather", None, True),
        ("demo/nested/alpha", None, True),
        # [] = 全不看
        ("general/weather", [], False),
        ("demo/nested/alpha", [], False),
        # 目录前缀（带 /）：含嵌套
        ("general/weather", ["general/"], True),
        ("general/foo/bar", ["general/"], True),
        ("demo/nested/alpha", ["general/"], False),
        ("demo/nested/alpha", ["demo/"], True),
        ("demo/nested/alpha", ["demo/nested/"], True),
        ("demo/other/leaf", ["demo/nested/"], False),
        # 精确匹配（无 / 结尾）
        ("demo/nested/alpha", ["demo/nested/alpha"], True),
        ("demo/nested/alpha/extra", ["demo/nested/alpha"], False),
        ("demo/nested/beta", ["demo/nested/alpha"], False),
        # 防 prefix 贪婪：general/ 不应误匹配 general_foo
        ("general_foo/bar", ["general/"], False),
        # 多前缀 OR
        ("general/weather", ["demo/", "general/"], True),
        ("demo/other/x", ["demo/", "general/"], True),
        ("ops/deploy", ["demo/", "general/"], False),
        # 空字符串或仅空白的 prefix 应被忽略
        ("general/weather", ["", " "], False),
        ("general/weather", ["", "general/"], True),
    ],
)
def test_is_skill_allowed(rel_name, allowed, expected):
    assert SkillTool._is_skill_allowed(rel_name, allowed) is expected


# ── _scan_skills 过滤（依赖真实 skills/ 目录）────────────────────────────


def test_scan_skills_all_visible_by_default():
    names = {s["name"] for s in SkillTool._scan_skills()}
    assert "general/weather" in names
    assert "demo/nested/alpha" in names
    assert "demo/nested/beta" in names


def test_scan_skills_general_only():
    names = {s["name"] for s in SkillTool._scan_skills(allowed_paths=["general/"])}
    assert names == {"general/weather"}


def test_scan_skills_demo_nested():
    names = {s["name"] for s in SkillTool._scan_skills(allowed_paths=["demo/nested/"])}
    assert names == {"demo/nested/alpha", "demo/nested/beta"}


def test_scan_skills_empty_list_hides_all():
    assert SkillTool._scan_skills(allowed_paths=[]) == []


def test_scan_skills_exact_match_only():
    names = {
        s["name"] for s in SkillTool._scan_skills(
            allowed_paths=["demo/nested/alpha"]
        )
    }
    assert names == {"demo/nested/alpha"}


# ── catalog_lines 同步过滤 ─────────────────────────────────────────────


def test_catalog_lines_matches_scan_filter():
    lines = SkillTool.catalog_lines(allowed_paths=["general/"])
    text = "\n".join(lines)
    assert "general/weather" in text
    assert "demo/nested/" not in text


# ── execute 运行时双端校验 ─────────────────────────────────────────────


def test_execute_allowed_skill_loads():
    tool = SkillTool()
    ctx = ToolContext(agent_name="general", allowed_skill_paths=["general/"])
    out = tool.execute({"skill": "general/weather"}, ctx)
    assert "[SKILL LOADED]" in out
    assert "general/weather" in out


def test_execute_denied_skill_refuses():
    tool = SkillTool()
    ctx = ToolContext(agent_name="general", allowed_skill_paths=["general/"])
    out = tool.execute({"skill": "demo/nested/alpha"}, ctx)
    assert out.startswith("错误：")
    assert "不存在或当前 Agent 无权访问" in out
    # "可用技能"列表里只包含当前 agent 可见的 skill，不泄露被禁的
    # （错误消息本身会回显用户传入的 skill 名，这是合理的）
    _, _, listing = out.partition("可用技能:")
    assert "general/weather" in listing
    assert "demo/nested/" not in listing
    assert "alpha" not in listing


def test_execute_no_restriction_loads_any():
    tool = SkillTool()
    ctx = ToolContext(agent_name="misc", allowed_skill_paths=None)
    out = tool.execute({"skill": "demo/nested/alpha"}, ctx)
    assert "[SKILL LOADED]" in out


def test_execute_empty_list_denies_all():
    tool = SkillTool()
    ctx = ToolContext(agent_name="paranoid", allowed_skill_paths=[])
    out = tool.execute({"skill": "general/weather"}, ctx)
    assert out.startswith("错误：")


def test_execute_path_traversal_blocked():
    """即使无 allow 限制，也不能越出 skills 根目录。"""
    tool = SkillTool()
    ctx = ToolContext(allowed_skill_paths=None)
    out = tool.execute({"skill": "../../etc/passwd"}, ctx)
    assert out.startswith("错误：")


# ── to_openai_schema 按 agent_config 调整 description ─────────────────


def test_schema_description_filters_by_agent_config():
    tool = SkillTool()

    class _MockCfg:
        allowed_skill_paths = ["general/"]

    schema = tool.to_openai_schema(agent_config=_MockCfg())
    desc = schema["function"]["description"]
    assert "general/weather" in desc
    assert "demo/nested/" not in desc


def test_schema_description_without_agent_config_shows_all():
    tool = SkillTool()
    schema = tool.to_openai_schema(agent_config=None)
    desc = schema["function"]["description"]
    assert "general/weather" in desc
    assert "demo/nested/alpha" in desc


# ── 与真实 agent 定义联动 ───────────────────────────────────────────────


def test_default_agents_see_all_skills():
    """默认（allowed_skill_paths=None）的 agent 能看到全部 skill。"""
    agents = load_agents()
    for name in ("general", "explore", "plan", "verify"):
        assert name in agents, f"missing agent definition: {name}"
        cfg = agents[name]
        assert cfg.allowed_skill_paths is None, (
            f"{name} should default to None (see all) but got {cfg.allowed_skill_paths}"
        )
        visible = {s["name"] for s in SkillTool._scan_skills(
            allowed_paths=cfg.allowed_skill_paths
        )}
        assert "general/weather" in visible
        assert "demo/nested/alpha" in visible
        assert "demo/nested/beta" in visible


def test_echo_tester_has_no_skill_access():
    """echo-tester 显式设为 allowed_skill_paths=[]，演示完全禁用 skill。"""
    agents = load_agents()
    cfg = agents["echo-tester"]
    assert cfg.allowed_skill_paths == []
    assert SkillTool._scan_skills(allowed_paths=cfg.allowed_skill_paths) == []


def test_demo_scoped_agent_sees_only_nested():
    """模拟细粒度 agent：用前缀收窄到 demo/nested/ 域。"""
    visible = {s["name"] for s in SkillTool._scan_skills(
        allowed_paths=["demo/nested/"]
    )}
    assert "demo/nested/alpha" in visible
    assert "demo/nested/beta" in visible
    assert "general/weather" not in visible
