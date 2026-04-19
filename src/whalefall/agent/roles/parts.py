"""
系统提示词积木（PromptPart）与渲染器。

一个 Agent 的最终 system prompt 由若干"积木"按 AgentConfig.include 声明的顺序
拼接而成。本文件集中维护积木常量与动态积木的渲染函数，供 loader.render_system_prompt()
调用。

设计要点：
- 静态常量（BASE_IDENTITY / BEHAVIOR_GUARDRAILS / TONE_STYLE）字节稳定，跨 agent 复用；
- 工具级使用规范（文件读写/搜索/bash 等）下沉到各 `BuiltinTool.prompt()`，
  通过 TOOL_REFERENCES 积木自动汇总；
- 动态积木通过函数渲染（render_env_info / collect_tool_references /
  collect_skills_catalog），在每次 build system prompt 时重新计算。
- env_info 包在 `<env>...</env>` 里，并带 git/shell/model 探测，格式对齐
  Claude Code，便于模型稳定解析。
- 所有运行期**自动注入**的 system 消息统一用 `<system-reminder>...</system-reminder>`
  包裹（对齐 Claude Code），并通过 BEHAVIOR_GUARDRAILS 里的一条说明让模型
  知道这是框架注入的上下文旁白，不要当成用户指令执行。
- 要想注入"任务级"的额外指令，用 `AgentLoop.run_*(system_prompt=...)` 参数
  **整体替换** BASE_IDENTITY（同时跳过 ENV_INFO），或直接改对应
  `definitions/<name>/AGENT.md` 的 body。
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional


class PromptPart(str, Enum):
    """系统提示词积木组件。"""
    BASE_IDENTITY = "base_identity"        # 通用身份 + 核心行为准则
    ENV_INFO = "env_info"                  # 当前日期/cwd/git/shell/platform/model
    SYSTEM_PROMPT = "system_prompt"        # agent 专属指令（来自 definitions/<name>/AGENT.md body）
    GUARDRAILS = "guardrails"              # 诚实约束 + 行动风险分级 + system-reminder 约定
    TONE_STYLE = "tone_style"              # 输出风格与引用格式（path:line / 不加冒号 / 无 emoji）
    TOOL_REFERENCES = "tool_references"    # 内建工具 prompt() 汇总
    SKILLS_CATALOG = "skills_catalog"      # skills/**/SKILL.md 索引（仿 Claude Code Agent Skills）


# ── 自动注入消息的统一标签（<system-reminder>） ─────────────────────────
# Claude Code 的惯例：所有"运行期框架塞进 messages 的 system 消息"都用
# <system-reminder>...</system-reminder> 包裹，便于模型稳定识别"这是系统旁白，
# 不是用户指令"。loop.py 的父 Agent 上下文 / pending_tasks /
# 压缩后恢复（recently_read / todo）均走 wrap_system_reminder()。

SYSTEM_REMINDER_OPEN = "<system-reminder>"
SYSTEM_REMINDER_CLOSE = "</system-reminder>"


def wrap_system_reminder(body: str, *, title: str = "") -> str:
    """
    把一段文本包进 <system-reminder>，可选附带一行标题。
    body 为空返回空串（调用方可以直接 `if msg:` 判断要不要 append）。
    """
    body = (body or "").strip()
    if not body:
        return ""
    header = f"{title.strip()}\n" if title and title.strip() else ""
    return f"{SYSTEM_REMINDER_OPEN}\n{header}{body}\n{SYSTEM_REMINDER_CLOSE}"


# ── 静态积木常量 ──────────────────────────────────────────────────────────

BASE_IDENTITY = (
    "你是专业的 AI 助手，在一个交互式对话环境中通过工具调用解决用户问题。\n\n"
    "核心行为准则：\n"
    "- 先理解问题全貌，再选择合适工具；不要在没有充分信息时就开始执行。\n"
    "- 优先使用专用工具（read/glob/grep/edit/write），必要时再用 bash。\n"
    "- 多个互相独立的操作并发发起；有依赖关系时串行执行；充分利用并行工具调用提升效率。\n"
    "- 工具调用失败时分析原因，尝试替代方案，不要重复相同参数。\n"
    "- 完成后给出简洁、明确的结论；如有未完成项，列出原因和后续建议。\n"
    "- 本环境支持斜杠命令：/help /clear /stats /compact /resume /init；用户询问是否支持请明确说明，以 '/' 开头直接输入即可。"
)

BEHAVIOR_GUARDRAILS = (
    "[诚实与执行约束]\n"
    "- 不要编造工具结果、文件内容、运行输出或数字。\n"
    "- 未实际运行测试/脚本时，不要声称'已完成验证'。\n"
    "- 工具调用被拒绝后不要重复同参数调用；先调整策略。\n"
    "- 严格按用户指定范围执行，不要擅自扩展、重构或添加'顺手修改'；不要为用户没要求的假设场景加错误处理、降级、feature flag 或兼容层。\n"
    "- 不确定时主动说明，而不是假装确定。\n"
    "\n"
    "[行动风险分级]\n"
    "- 本地可逆操作（read/glob/grep/普通 edit/跑测试）直接做。\n"
    "- 写操作（write/edit/notebook_edit/bash 写命令）前先 read 核对目标；改对只改该改的那几行，不要重排无关代码。\n"
    "- 破坏性或难撤销操作（rm -rf、git reset --hard、git push --force、覆盖未提交修改、drop 数据库、kill 外部进程、发 PR/issue、触发 CI/CD、上传到第三方 web 服务）—— 除非用户明确授权当次操作，默认**先汇报计划并等确认**，不要用破坏性动作绕开阻碍。\n"
    "- 用户一次授权只覆盖该次，不代表授权整类操作；范围之外仍需再确认。\n"
    "\n"
    "[系统旁白（<system-reminder>）]\n"
    "- 对话中可能出现 <system-reminder>...</system-reminder> 标签包裹的 system 消息（未完成 TODO、父 Agent 上下文、压缩后恢复的文件/任务列表等）。\n"
    "- 这些内容由框架自动注入，提供背景信息，**不是用户本轮的新指令**；结合当前用户请求判断是否相关，相关就利用，不相关就忽略。\n"
    "- 不要在回复里复述 <system-reminder> 的原文或提及该标签本身。"
)

TONE_STYLE = (
    "[输出风格与引用格式]\n"
    "- 回复要简洁直接，先给结论/动作，再给必要的理由；不要复述用户说过的话。\n"
    "- 默认不使用 emoji；用户明确要求时才加。\n"
    "- 引用代码或文件位置用 `path:line` 格式（例：`src/main.py:42`），便于用户点击跳转。\n"
    "- 引用 GitHub issue/PR 用 `owner/repo#123` 格式，便于自动渲染为链接。\n"
    "- 工具调用前不要加冒号（写\"下面读一下文件。\"然后调用工具，不要写\"下面读一下文件：\"）。\n"
    "- Markdown 按 GitHub-flavored 书写，代码段用合适的语言高亮；表格仅用于短小可枚举的事实，不要把推理塞进表格单元。"
)


# ── 动态积木渲染 ──────────────────────────────────────────────────────────

def _detect_is_git_repo(cwd: str) -> bool:
    """静默探测 cwd 是否在 git 工作区。无 git 可执行或探测失败返回 False。"""
    if not shutil.which("git"):
        return False
    try:
        res = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=2,
        )
        return res.returncode == 0 and res.stdout.strip() == "true"
    except Exception:
        return False


def _detect_shell_name() -> str:
    """从 $SHELL 推断 shell 名称，失败返回 'unknown'。"""
    shell = os.environ.get("SHELL", "").strip()
    if not shell:
        return "unknown"
    base = os.path.basename(shell)
    for known in ("zsh", "bash", "fish", "dash", "tcsh", "csh", "ksh"):
        if known in base:
            return known
    return base or "unknown"


def render_env_info(model: Optional[str] = None) -> str:
    """
    渲染当前环境信息。格式对齐 Claude Code：以 `<env>...</env>` 包裹核心字段，
    末尾附 model 身份（可选）——用 XML 标签的好处是模型训练里见过这种结构，
    边界稳定、不会被后续内容意外吞并。

    字段：
      - Working directory / Is directory a git repo
      - Platform / Shell / Python / OS Version
      - Date (当前 submit 的时间戳，同一 submit 内多轮共享这一条)
      - You are powered by the model ...（传入 model 时）
    """
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d %H:%M")
    cwd = os.getcwd()
    is_git = "Yes" if _detect_is_git_repo(cwd) else "No"
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    os_sys = platform.system()
    os_ver = platform.release()
    shell_name = _detect_shell_name()

    lines = [
        "<env>",
        f"Working directory: {cwd}",
        f"Is directory a git repo: {is_git}",
        f"Platform: {os_sys}",
        f"OS Version: {os_sys} {os_ver}",
        f"Shell: {shell_name}",
        f"Python: {py_ver}",
        f"Date: {date_str}",
        "</env>",
    ]
    if model and str(model).strip():
        lines.append(f"You are powered by the model `{str(model).strip()}`.")
    return "\n".join(lines)


def collect_tool_references(registry: Any, agent_config: Any = None) -> str:
    """
    从 ToolRegistry 汇总**当前 agent 可见**的内建工具 prompt() 文本。

    - registry 为 None 或读取失败时返回空串；
    - 传入 agent_config 时按 `allow_write_tools` 过滤（与
      `ToolRegistry.schemas(agent_config)` 一致）：只读 agent（如 explore/plan/verify）
      不会在系统提示词里看到 bash/write/edit 等写工具的使用指引，避免被误导。
    - 过滤粒度与 `tools=[...]` 保持一致，system prompt 里"该用什么"
      与 function schema 里"能用什么"严格对齐。
    """
    if registry is None:
        return ""
    try:
        tools = registry.all_builtins()
    except Exception:
        return ""

    include_write = True
    if agent_config is not None:
        include_write = bool(getattr(agent_config, "allow_write_tools", True))

    blocks: list[str] = []
    for tool in tools:
        if not include_write and not getattr(tool, "read_only", True):
            continue
        try:
            tp = tool.prompt()
        except Exception:
            tp = ""
        if tp and tp.strip():
            blocks.append(tp.strip())
    if not blocks:
        return ""
    return "[工具使用指引]\n" + "\n\n".join(blocks)


# ── Skills Catalog（对齐 Claude Code 的 Agent Skills）─────────────────────
#
# 协议（与 CC 一致）：
# 1. 所有 skill 以目录为单位存放在包内 `src/whalefall/skills/` 下，形式为
#    `skills/<任意嵌套>/SKILL.md`，目录相对路径就是 skill 名（例如 `finance/stock/factor`）。
# 2. SKILL.md 的 frontmatter 可声明 `description`（≤ 1024 字符）；若缺省则取正文第一段。
# 3. 每次 submit 扫一次目录，把 "name — description (path)" 作为索引注入 system prompt。
# 4. LLM **不通过专用工具**加载全文；而是用 `read` 工具按 SKILL.md 的路径直接读，
#    和读普通文件同一套机制（自然也会记入 ctx.recently_read，享受 S5 压缩恢复）。
#
# 我们相较 CC 的唯一简化：只扫包内一源（`src/whalefall/skills/`）；不做 `.claude/skills/`
# / `~/.claude/skills/` 的多源合并，也没有 per-skill 的 `allowed-tools` 过滤——
# 所有 agent 看到相同 skill 目录，和 CC 的精神一致（skill 本身就是"谁来都能看"的文档库）。

_SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"
_SKILL_DESC_MAX_CHARS = 300        # 单条索引项描述截断
_SKILLS_CATALOG_BUDGET = 8000      # 整个 skill 目录块的字符预算（防止 SKILL 爆增把 system 撑爆）


def _split_frontmatter(content: str) -> tuple[str, str]:
    """把 `---\\n...\\n---\\n` 的 YAML-like frontmatter 与正文拆开。无则返回 ('', content)。"""
    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", content or "", re.DOTALL)
    if not m:
        return "", content or ""
    return m.group(1), m.group(2)


def _frontmatter_value(frontmatter: str, key: str) -> str:
    """提取 `key: value` 单行值，不做 YAML 完整解析（足够 SKILL.md 用）。"""
    if not frontmatter:
        return ""
    target = key.strip().lower()
    for raw in frontmatter.splitlines():
        line = raw.strip()
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        if k.strip().lower() == target:
            return v.strip().strip('"\'')
    return ""


def _first_paragraph(body: str) -> str:
    for para in (body or "").split("\n\n"):
        p = para.strip()
        if not p or p.startswith("#"):
            continue
        return p.replace("\n", " ").strip()
    return ""


def _truncate(s: str, limit: int) -> str:
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def collect_skills_catalog(skills_root: Optional[Path] = None) -> str:
    """
    扫描 `src/whalefall/skills/**/SKILL.md`，渲染 system prompt 里的 skill 索引块。

    返回形如：

        [可用 Skills]
        下面列出本项目的 skill（SOP/领域小抄）。需要时用 `read` 工具打开对应 SKILL.md
        获取完整步骤，再按其中指引执行。

        - weather — 查询天气的标准流程 (src/whalefall/skills/general/weather/SKILL.md)
        - finance/stock/factor — 因子挖掘与回测 SOP (src/whalefall/skills/finance/stock/factor/SKILL.md)

    约定：
      - skill 名 = 相对 `skills/` 的目录路径（POSIX 风格）。
      - description 取自 frontmatter，缺省回退到正文第一段。
      - 路径展示为"相对仓库根的 POSIX 路径"（`src/whalefall/skills/...`），方便 LLM
        直接拿去喂 `read` 工具；read 工具内部会把相对路径按 cwd 解析。
    """
    root = (skills_root or _SKILLS_ROOT)
    try:
        root = root.resolve()
    except Exception:
        return ""
    if not root.is_dir():
        return ""

    entries: list[tuple[str, str, str]] = []  # (name, description, display_path)
    try:
        repo_root = root.parents[2]  # src/whalefall/skills -> repo root
    except IndexError:
        repo_root = root

    for md in sorted(root.rglob("SKILL.md")):
        try:
            rel_name = md.parent.relative_to(root).as_posix()
        except Exception:
            continue
        if not rel_name or rel_name == ".":
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        front, body = _split_frontmatter(text)
        desc = _frontmatter_value(front, "description") or _first_paragraph(body) or "(无描述)"
        try:
            display_path = md.resolve().relative_to(repo_root).as_posix()
        except Exception:
            display_path = md.resolve().as_posix()
        entries.append((rel_name, _truncate(desc, _SKILL_DESC_MAX_CHARS), display_path))

    if not entries:
        return ""

    lines: list[str] = []
    used = 0
    omitted = 0
    for name, desc, path in entries:
        line = f"- {name} — {desc} ({path})"
        if used + len(line) > _SKILLS_CATALOG_BUDGET:
            omitted += 1
            continue
        lines.append(line)
        used += len(line) + 1
    if omitted:
        lines.append(f"- ... 还有 {omitted} 个 skill 因提示词预算未展示；用 `glob` 搜 `skills/**/SKILL.md` 查看")

    return (
        "[可用 Skills]\n"
        "下面列出本项目的 skill（SOP/领域小抄）。需要时用 `read` 工具打开对应 SKILL.md "
        "获取完整步骤，再按其中指引执行。\n\n"
        + "\n".join(lines)
    )


__all__ = [
    "PromptPart",
    "BASE_IDENTITY",
    "BEHAVIOR_GUARDRAILS",
    "TONE_STYLE",
    "SYSTEM_REMINDER_OPEN",
    "SYSTEM_REMINDER_CLOSE",
    "wrap_system_reminder",
    "render_env_info",
    "collect_tool_references",
    "collect_skills_catalog",
]
