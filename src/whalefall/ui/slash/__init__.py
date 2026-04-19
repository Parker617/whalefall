"""
whalefall.ui.slash —— 斜杠命令共享实现。

CLI (`ui/cli.py`) 与 Web (`ui/web.py`) 的命令解析、会话管理、帮助文本
集中到这里，避免两处实现逐项 drift。

公共命令：/clear /compact /resume /init /stats
CLI 专属（在 cli.py 里直接处理）：/exit /model /agent
Web 专属（在 web.py 里直接处理）：/reset（/clear 别名）
"""
from whalefall.ui.slash.core import (
    COMMON_HELP_LINES,
    SlashContext,
    SlashResult,
    cmd_clear,
    cmd_compact,
    cmd_init,
    cmd_resume,
    cmd_resume_last,
    cmd_stats,
    dispatch_common,
    format_session_list,
    normalize_slash_input,
    parse_slash,
)

__all__ = [
    "COMMON_HELP_LINES",
    "SlashContext",
    "SlashResult",
    "cmd_clear",
    "cmd_compact",
    "cmd_init",
    "cmd_resume",
    "cmd_resume_last",
    "cmd_stats",
    "dispatch_common",
    "format_session_list",
    "normalize_slash_input",
    "parse_slash",
]
