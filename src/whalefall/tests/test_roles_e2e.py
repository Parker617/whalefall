"""
端到端自检：agent/roles 加载 + system prompt 渲染 + ToolRegistry 过滤。

不启动 LLM，不跑工具；只在本地验证：
  1. load_agents() 能找到 4 个内建 + 至少 1 个 custom
  2. 每个 agent 的 system prompt 渲染不报错、非空、含 include 声明所需的标志段
  3. get_agent("not-exist") 回退 general，不抛错
  4. ToolRegistry.schemas(agent_config=...) 按 allow_write_tools 过滤工具

运行：
  python -m whalefall.tests.test_roles_e2e
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path


def _header(title: str) -> None:
    bar = "─" * 70
    print(f"\n{bar}\n{title}\n{bar}")


def _check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  [PASS] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        raise AssertionError(msg)


def main() -> int:
    from whalefall.agent.roles import (
        AgentConfig,
        PromptPart,
        get_agent,
        list_agent_names,
        load_agents,
        render_system_prompt,
    )
    from whalefall.tools.registry import build_default_registry

    # ── 1. 定义目录加载 ───────────────────────────────────────────────────
    _header("1. load_agents() 扫描 definitions/")
    agents = load_agents()
    names = sorted(agents.keys())
    print(f"  发现 agents: {names}")
    for required in ("general", "explore", "plan", "verify"):
        _check(required in agents, f"内建 agent '{required}' 已加载")
    _check(
        any(n not in ("general", "explore", "plan", "verify") for n in names),
        "至少一个自定义 agent 存在（用于端到端验证）",
    )

    _check(list_agent_names() == names, "list_agent_names() 与 load_agents() 一致")

    # ── 2. get_agent() 回退 ──────────────────────────────────────────────
    _header("2. get_agent() 默认与回退")
    general = get_agent("general")
    _check(general.name == "general", "get_agent('general') 正确")
    fallback = get_agent("this-does-not-exist-xyz")
    _check(fallback.name == "general", "未知 agent 回退到 general")

    # ── 3. 字段校验 ──────────────────────────────────────────────────────
    _header("3. 4 个内建 agent 字段校验")
    # 三态语义：None=全开 / []=全禁 / 非空列表=白名单
    exp = {
        "general": dict(allow_write_tools=True,  allow_subagent=True,  allowed_mcp_servers=None, max_turns=100),
        "explore": dict(allow_write_tools=False, allow_subagent=False, allowed_mcp_servers=None, max_turns=80),
        "plan":    dict(allow_write_tools=False, allow_subagent=False, allowed_mcp_servers=[],   max_turns=50),
        "verify":  dict(allow_write_tools=False, allow_subagent=False, allowed_mcp_servers=None, max_turns=40),
    }
    for name, spec in exp.items():
        cfg = agents[name]
        for k, v in spec.items():
            _check(getattr(cfg, k) == v, f"{name}.{k} == {v!r}")

    # ── 4. 渲染 system prompt（传入 registry 以汇总 tool.prompt()）──────
    _header("4. render_system_prompt() 含 registry")
    registry = build_default_registry()
    for name in ("general", "explore", "plan", "verify"):
        cfg = agents[name]
        prompt = render_system_prompt(cfg, registry=registry)
        _check(bool(prompt.strip()), f"{name}: 渲染非空")
        _check(
            "核心行为准则" in prompt,
            f"{name}: 含 BASE_IDENTITY（核心行为准则）",
        )
        _check(
            "[诚实与执行约束]" in prompt,
            f"{name}: 含 GUARDRAILS（诚实与执行约束）",
        )
        if PromptPart.TOOL_REFERENCES in cfg.include:
            _check(
                "[工具使用指引]" in prompt,
                f"{name}: 含 TOOL_REFERENCES（各工具 prompt 汇总）",
            )
        if PromptPart.SYSTEM_PROMPT in cfg.include and cfg.system_prompt:
            # 定义正文关键字检验（每个子 agent 的定义中都有 [X Mode] 标签）
            tag = {
                "explore": "[Explore Mode]",
                "plan": "[Plan Mode]",
                "verify": "[Verify Mode]",
            }.get(name)
            if tag:
                _check(tag in prompt, f"{name}: 含 SYSTEM_PROMPT 标签 {tag}")

    # custom agent
    _header("5. custom agent 渲染")
    custom_names = [n for n in names if n not in ("general", "explore", "plan", "verify")]
    for cn in custom_names:
        cfg = agents[cn]
        prompt = render_system_prompt(cfg, registry=registry)
        print(f"  custom='{cn}', include={[p.value for p in cfg.include]}")
        _check(bool(prompt.strip()), f"{cn}: 渲染非空")

    # ── 5. ToolRegistry 过滤 ─────────────────────────────────────────────
    _header("6. ToolRegistry.schemas() 按 allow_write_tools 过滤")
    reg = build_default_registry(
        include_agent_tool=False,   # 避免构造 AgentLoop 依赖
        include_web_search=False,
        include_web_browser=False,
    )
    full = reg.schemas(agent_config=agents["general"])
    readonly = reg.schemas(agent_config=agents["explore"])
    print(f"  general 工具数: {len(full)}")
    print(f"  explore 工具数: {len(readonly)}")
    _check(len(readonly) < len(full), "只读 agent 返回的工具数更少")
    readonly_names = {t.get("function", {}).get("name", "") for t in readonly}
    # 写工具必须被过滤掉
    for forbidden in ("bash", "write", "edit", "notebook_edit"):
        _check(
            forbidden not in readonly_names,
            f"只读 agent 不应看到写工具 '{forbidden}'",
        )

    # ── 6. 完整 prompt 预览（方便人工肉眼检查）──────────────────────────
    _header("7. system prompt 预览（前 500 字符）")
    for name in ("general", "explore", "plan", "verify", *custom_names):
        prompt = render_system_prompt(agents[name], registry=reg)
        preview = prompt[:500].replace("\n", "\n    ")
        print(f"\n  === {name} (len={len(prompt)}) ===\n    {preview}")

    print("\n★ 全部断言通过 ★")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\n✗ 断言失败: {exc}", file=sys.stderr)
        sys.exit(1)
