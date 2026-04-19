"""
系统提示词积木（PromptPart）与渲染器。

一个 Agent 的最终 system prompt 由若干"积木"按 AgentConfig.include 声明的顺序
拼接而成。本文件集中维护积木常量与动态积木的渲染函数，供 loader.render_system_prompt()
调用。

设计要点：
- 静态常量（BASE_IDENTITY / BEHAVIOR_GUARDRAILS）保持跨 agent 复用；
- 工具级使用规范（文件读写/搜索/bash 等）下沉到各 `BuiltinTool.prompt()`，
  通过 TOOL_REFERENCES 积木自动汇总；
- 动态积木通过函数渲染（render_env_info / collect_tool_references），
  在每次 build system prompt 时重新计算（环境信息、工具 prompt() 等）。
- 要想注入"任务级"的额外指令，用 `AgentLoop.run_*(system_prompt=...)` 参数
  **整体替换** BASE_IDENTITY（同时跳过 ENV_INFO），或直接改对应
  `definitions/<name>/AGENT.md` 的 body。
"""
from __future__ import annotations

import os
import platform
import sys
from datetime import datetime
from enum import Enum
from typing import Any


class PromptPart(str, Enum):
    """系统提示词积木组件。"""
    BASE_IDENTITY = "base_identity"      # 通用身份 + 核心行为准则
    ENV_INFO = "env_info"                # 当前日期/cwd/平台 + 斜杠命令提示
    SYSTEM_PROMPT = "system_prompt"      # agent 自己的 system_prompt 正文（来自 definitions/<name>/AGENT.md body）
    GUARDRAILS = "guardrails"            # 通用诚实约束 + 写操作前置检查
    TOOL_REFERENCES = "tool_references"  # 内建工具 prompt() 汇总


# ── 静态积木常量 ──────────────────────────────────────────────────────────

BASE_IDENTITY = (
    "你是专业的 AI 助手，在一个交互式对话环境中通过工具调用解决用户问题。\n\n"
    "核心行为准则：\n"
    "- 先理解问题全貌，再选择合适工具；不要在没有充分信息时就开始执行。\n"
    "- 优先使用专用工具（read/glob/grep/edit/write），必要时再用 bash。\n"
    "- 多个互相独立的操作并发发起；有依赖关系时串行执行。\n"
    "- 工具调用失败时分析原因，尝试替代方案，不要重复相同参数。\n"
    "- 完成后给出简洁、明确的结论；如有未完成项，列出原因和后续建议。"
)

BEHAVIOR_GUARDRAILS = (
    "[诚实与执行约束]\n"
    "- 不要编造工具结果、文件内容、运行输出或数字。\n"
    "- 未实际运行测试/脚本时，不要声称'已完成验证'。\n"
    "- 工具调用被拒绝后不要重复同参数调用；先调整策略。\n"
    "- 严格按用户指定范围执行，不要擅自扩展、重构或添加'顺手修改'。\n"
    "- 写操作（write/edit/notebook_edit/bash 写命令）前先确认目标与影响范围，必要时先 read 核对。\n"
    "- 不确定时主动说明，而不是假装确定。"
)


# ── 动态积木渲染 ──────────────────────────────────────────────────────────

def render_env_info() -> str:
    """渲染当前环境信息（日期/工作目录/平台/斜杠命令）。"""
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d %H:%M")
    cwd = os.getcwd()
    py_ver = f"Python {sys.version_info.major}.{sys.version_info.minor}"
    os_info = f"{platform.system()} {platform.release()}"
    return (
        "当前环境信息："
        f"\n- 日期时间：{date_str}"
        f"\n- 工作目录：{cwd}"
        f"\n- 平台：{os_info} / {py_ver}"
        "\n- 本环境支持斜杠命令：/help /clear /stats /compact /resume /init；"
        "用户若询问是否支持，请明确说明支持，命令需以 '/' 开头直接输入。"
    )


def collect_tool_references(registry: Any) -> str:
    """
    从 ToolRegistry 汇总所有内建工具的 prompt() 文本。
    registry 为 None 或读取失败时返回空串。
    """
    if registry is None:
        return ""
    try:
        tools = registry.all_builtins()
    except Exception:
        return ""
    blocks: list[str] = []
    for tool in tools:
        try:
            tp = tool.prompt()
        except Exception:
            tp = ""
        if tp and tp.strip():
            blocks.append(tp.strip())
    if not blocks:
        return ""
    return "[工具使用指引]\n" + "\n\n".join(blocks)


__all__ = [
    "PromptPart",
    "BASE_IDENTITY",
    "BEHAVIOR_GUARDRAILS",
    "render_env_info",
    "collect_tool_references",
]
