"""
斜杠命令核心：解析 + 公共命令实现（/clear /compact /resume /init /stats）。

所有依赖 QueryEngine / session_id 的业务逻辑都集中在这里，让 CLI/Web
只负责"输入 → 调用 → 输出"的壳子。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple


AGENT_MD_TEMPLATE = (
    "# AGENT.md\n\n"
    "本文件用于定义当前项目中 agent 的工作约定。\n\n"
    "可填写内容示例：\n"
    "- 项目背景\n"
    "- 编码规范\n"
    "- 测试与验证要求\n"
    "- 禁止事项\n"
)


COMMON_HELP_LINES: Tuple[str, ...] = (
    "/clear             清空当前会话上下文",
    "/compact           手动执行一次 microcompact",
    "/resume [id]       列出最近会话或恢复指定会话",
    "/init              在当前工作目录创建 AGENT.md（若不存在）",
    "/stats             显示当前会话统计",
    "/help              显示此帮助",
)


def normalize_slash_input(query: str) -> str:
    """
    归一化斜杠命令输入，兼容中文输入法常见字符。

    处理项：
    - 全角斜杠 "／" -> "/"
    - 零宽字符移除（避免命令匹配失败）
    - 前后空白裁剪
    """
    text = query or ""
    text = text.replace("／", "/")
    text = text.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    return text.strip()


def parse_slash(text: str) -> Tuple[str, str]:
    """把斜杠输入拆成 (command, arg)，command 已小写；非斜杠输入返回 ("", text)。"""
    normalized = normalize_slash_input(text)
    if not normalized.startswith("/"):
        return "", normalized
    parts = normalized.split(None, 1)
    command = (parts[0] if parts else "").lower()
    arg = (parts[1] if len(parts) > 1 else "").strip()
    return command, arg


@dataclass
class SlashContext:
    """
    斜杠命令运行上下文。

    必填：
      - query_engine: QueryEngine 实例（/clear /compact /resume /stats 都需要）
      - session_id  : 当前会话 id

    可选：
      - strict_cold_start : True 时禁用 /resume（Web 冷启动模式）
      - extra_stats_fn    : 调用时产出"壳子"特有的统计行（如 CLI 的 turns、tool calls）
      - cwd               : /init 目标目录；默认 os.getcwd()
    """
    query_engine: Any
    session_id: str
    strict_cold_start: bool = False
    extra_stats_fn: Optional[Callable[[], Dict[str, Any]]] = None
    cwd: Optional[str] = None


@dataclass
class SlashResult:
    """
    斜杠命令执行结果。

    字段：
      - handled   : 是否被处理（False 表示非斜杠命令或无法处理）
      - message   : 给用户的一条消息（空字符串表示"已完成、无需输出"）
      - cleared   : /clear 执行成功；CLI 可借此重新印欢迎语
      - should_exit : /exit 等退出类命令；由调用方负责处理
    """
    handled: bool = False
    message: str = ""
    cleared: bool = False
    should_exit: bool = False


def format_session_list(sessions: List[Dict[str, Any]], current_session_id: str) -> List[str]:
    """把 QueryEngine.list_sessions() 结果格式化成可读行列表。"""
    lines = ["最近会话（输入 /resume <session_id> 恢复）："]
    for s in sessions:
        ts = datetime.fromtimestamp(s["updated_at"]).strftime("%Y-%m-%d %H:%M")
        marker = " [当前]" if s.get("session_id") == current_session_id else ""
        lines.append(
            f"  {s['session_id']:<36}  {s['turns']:>3} 轮  {ts}{marker}"
        )
    return lines


# ── 具体命令 ─────────────────────────────────────────────────────────────

def cmd_clear(ctx: SlashContext) -> SlashResult:
    if ctx.query_engine is None:
        return SlashResult(handled=True, message="无 QueryEngine，无法清空会话。")
    ctx.query_engine.clear_session(ctx.session_id)
    return SlashResult(handled=True, message="会话上下文已清空。", cleared=True)


def cmd_compact(ctx: SlashContext) -> SlashResult:
    if ctx.query_engine is None:
        return SlashResult(handled=True, message="无 QueryEngine，跳过压缩。")
    n = ctx.query_engine.compact_session(ctx.session_id)
    return SlashResult(
        handled=True,
        message=f"Context 已压缩，当前上下文消息数: {n}",
    )


def cmd_resume(ctx: SlashContext, arg: str) -> SlashResult:
    if ctx.query_engine is None:
        return SlashResult(handled=True, message="无 QueryEngine，无法恢复会话。")
    if ctx.strict_cold_start:
        return SlashResult(
            handled=True,
            message="冷启动模式下 /resume 不可用（不保留跨刷新会话历史）。",
        )
    if not arg:
        sessions = ctx.query_engine.list_sessions(limit=15)
        if not sessions:
            return SlashResult(handled=True, message="暂无历史会话。")
        return SlashResult(
            handled=True,
            message="\n".join(format_session_list(sessions, ctx.session_id)),
        )
    n = ctx.query_engine.load_session_into(arg, ctx.session_id)
    if n <= 0:
        return SlashResult(handled=True, message=f"会话 '{arg}' 不存在或为空。")
    return SlashResult(
        handled=True,
        message=f"已恢复会话 '{arg}'，载入 {n} 条消息。",
    )


def cmd_init(ctx: SlashContext) -> SlashResult:
    target_dir = ctx.cwd or os.getcwd()
    target = os.path.join(target_dir, "AGENT.md")
    if os.path.exists(target):
        return SlashResult(handled=True, message=f"AGENT.md 已存在: {target}")
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(AGENT_MD_TEMPLATE)
    except OSError as exc:
        return SlashResult(handled=True, message=f"创建 AGENT.md 失败: {exc}")
    return SlashResult(handled=True, message=f"已创建 AGENT.md: {target}")


def cmd_stats(ctx: SlashContext) -> SlashResult:
    turns_in_memory = (
        ctx.query_engine.get_session_turns(ctx.session_id)
        if ctx.query_engine is not None
        else 0
    )
    lines = [
        "会话统计：",
        f"  session_id   : {ctx.session_id}",
        f"  上下文轮数   : {turns_in_memory}",
    ]
    if ctx.extra_stats_fn is not None:
        try:
            extra = ctx.extra_stats_fn() or {}
        except Exception:
            extra = {}
        for k, v in extra.items():
            lines.append(f"  {k:<12}: {v}")
    return SlashResult(handled=True, message="\n".join(lines))


# ── 公共 dispatcher（CLI/Web 通用） ──────────────────────────────────────

def dispatch_common(text: str, ctx: SlashContext) -> SlashResult:
    """
    处理 CLI/Web 通用斜杠命令。

    返回 `SlashResult`：
      - handled=False 表示非斜杠或未命中，调用方自行处理（/help /exit /model /agent ...）
      - handled=True  表示已处理（message 为输出内容，可能为空）
    """
    command, arg = parse_slash(text)
    if not command.startswith("/"):
        return SlashResult(handled=False)

    if command in ("/clear", "/reset"):
        return cmd_clear(ctx)
    if command == "/compact":
        return cmd_compact(ctx)
    if command == "/resume":
        return cmd_resume(ctx, arg)
    if command == "/init":
        return cmd_init(ctx)
    if command == "/stats":
        return cmd_stats(ctx)

    return SlashResult(handled=False)
