"""
系统提示词装配（render_system_prompt）回归测试。

覆盖点：
  - 默认 general agent 装配后含 BASE_IDENTITY / ENV_INFO / GUARDRAILS /
    TONE_STYLE / TOOL 块
  - `custom_base` 参数整体替换 BASE_IDENTITY 并跳过 ENV_INFO
  - render_system_prompt 不会读取非 skill 的任何文件（零嗅探不变量）
  - ENV_INFO 输出 <env>...</env> XML 包裹，且被隔离在 dynamic 段（末尾）
  - <system-reminder> 标签约定与 wrap_system_reminder 行为
  - SKILLS_CATALOG 扫 `src/whalefall/skills/**/SKILL.md` 渲染索引
  - 子 agent（explore/plan/verify/echo-tester）默认能装配不抛错
"""
from __future__ import annotations

from pathlib import Path

import pytest

from whalefall.agent.roles import (
    PromptPart,
    collect_skills_catalog,
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


def test_render_system_prompt_only_touches_skill_files(monkeypatch) -> None:
    """
    零嗅探（修订版）：render_system_prompt 只允许读 `src/whalefall/skills/` 下的
    SKILL.md；其他路径的 read_text 都视为"文件嗅探"回归。

    说明：原先版本完全禁止磁盘 I/O，但 SKILLS_CATALOG 需要扫 SKILL.md；
    所以把限制收敛到"仅 skills 目录"。env_info 的 `git rev-parse` 走 subprocess，
    不经 Path.read_text，仍与此不变量互不冲突。
    """
    import pathlib

    agent = get_agent("general")
    skills_root = (Path(__file__).resolve().parents[1] / "skills").resolve()
    original_read_text = pathlib.Path.read_text

    def _guarded_read_text(self, *a, **kw):
        try:
            resolved = self.resolve()
        except Exception:
            resolved = self
        # 只允许读 skills/**/SKILL.md
        try:
            resolved.relative_to(skills_root)
        except ValueError:
            raise AssertionError(
                f"render_system_prompt leaked file I/O outside skills/: {self}"
            )
        if resolved.name != "SKILL.md":
            raise AssertionError(
                f"render_system_prompt read non-SKILL.md under skills/: {self}"
            )
        return original_read_text(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "read_text", _guarded_read_text)
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


# ── SKILLS_CATALOG（仿 CC Agent Skills）────────────────────────────────

def test_collect_skills_catalog_from_real_repo_has_entries() -> None:
    """
    仓库内置 skills/demo/nested/{alpha,beta}/SKILL.md + skills/general/weather/SKILL.md，
    collect_skills_catalog 应能产出非空索引并给出正确的 "name — description (path)" 行。
    """
    out = collect_skills_catalog()
    assert out, "skills/ 目录下有 SKILL.md 时 catalog 不能为空"
    assert "[可用 Skills]" in out
    # 至少命中一条内置 demo skill
    assert "demo/nested/alpha" in out or "demo/nested/beta" in out
    # 指路径必须能直接给 read 工具用（POSIX 相对仓库根）
    assert "src/whalefall/skills/" in out


def test_collect_skills_catalog_empty_root(tmp_path: Path) -> None:
    assert collect_skills_catalog(skills_root=tmp_path) == ""


def test_collect_skills_catalog_custom_root(tmp_path: Path) -> None:
    (tmp_path / "foo" / "bar").mkdir(parents=True)
    (tmp_path / "foo" / "bar" / "SKILL.md").write_text(
        "---\ndescription: fake skill\n---\nbody here\n",
        encoding="utf-8",
    )
    out = collect_skills_catalog(skills_root=tmp_path)
    assert "[可用 Skills]" in out
    assert "foo/bar" in out
    assert "fake skill" in out


def test_render_system_prompt_contains_skills_catalog_in_static() -> None:
    """SKILLS_CATALOG 属于静态块，应出现在 <env> 之前。"""
    agent = get_agent("general")
    static_part, dyn_part = render_system_prompt_split(agent, registry=None)
    assert "[可用 Skills]" in static_part
    assert "<env>" in dyn_part


def test_render_system_prompt_mcp_client_does_not_affect_prompt() -> None:
    """
    MCP 通道已纯化：system prompt 里不再聚合 server-level instructions，无论是否
    传入 mcp_client，渲染结果都完全一致。
    """
    agent = get_agent("general")

    class _FakeMCPClient:
        def list_tools(self, servers=None):  # pragma: no cover - 不应被调用
            return []

    out_no_mcp = render_system_prompt(agent, registry=None, mcp_client=None)
    out_with_mcp = render_system_prompt(agent, registry=None, mcp_client=_FakeMCPClient())
    assert out_no_mcp == out_with_mcp
    assert "[MCP Server 使用说明]" not in out_no_mcp
