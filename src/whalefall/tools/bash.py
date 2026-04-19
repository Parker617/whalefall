"""
BashTool：执行 shell 命令的内建工具。

- read_only=False（写工具，串行执行）
- 参数：command(str), timeout(int=30), description(str 可选)
- 超时处理，输出截断（MAX_OUTPUT=10000 字符）
- 安全检查：拦截极端危险命令
- 返回：stdout + stderr + 退出码
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Dict

from whalefall.tools.base import BuiltinTool, ToolContext

MAX_OUTPUT = 10_000      # 最大输出字符数
DEFAULT_TIMEOUT = 30     # 默认超时秒数

# 极端危险命令模式（正则）
_DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/\s*$",            # rm -rf /
    r"rm\s+-rf\s+/\*",              # rm -rf /*
    r":\(\)\s*\{",                   # fork bomb :(){ :|:& };:
    r">\s*/dev/sda",                # 磁盘覆盖
    r"mkfs\.",                       # 格式化文件系统
    r"shutdown\s+",                  # 系统关机
    r"reboot\s*$",                  # 重启
    r"halt\s*$",                    # 停机
    r"dd\s+if=.*of=/dev/[sh]d",     # dd 写磁盘
    r"chmod\s+-R\s+777\s+/\s*$",   # chmod 777 根目录
]
_DANGEROUS_RE = re.compile("|".join(_DANGEROUS_PATTERNS), re.IGNORECASE)


class BashTool(BuiltinTool):
    """执行 shell 命令。read_only=False，串行执行。"""

    name = "bash"
    description = (
        "在 shell 中执行命令并返回输出。支持管道、重定向等所有 bash 语法。"
        "输出超过 10000 字符时自动截断。超时默认 30 秒。"
        "危险命令（如 rm -rf /）会被拒绝执行。"
    )
    read_only = False
    parameters_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令",
            },
            "timeout": {
                "type": "integer",
                "description": "命令超时秒数，默认 30 秒，最大 300 秒",
                "default": 30,
            },
        },
        "required": ["command"],
    }

    def prompt(self) -> str:
        return (
            "Shell 命令（bash）：\n"
            "- 只用于真正的 shell 操作（git/npm/测试/构建等），不要替代 read/write/edit/glob/grep。\n"
            "- 独立命令并行发起，有依赖的用 && 串行；不要用 ; 连成一行。\n"
            "- 长任务带合理的 timeout；危险命令（rm -rf /、写磁盘等）会被直接拒绝。"
        )

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        command = args.get("command", "").strip()
        timeout = min(int(args.get("timeout") or DEFAULT_TIMEOUT), 300)

        if not command:
            return "错误：command 参数不能为空"

        # 安全检查
        safety_err = self._check_safety(command)
        if safety_err:
            return safety_err

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=os.getcwd(),
                env=os.environ.copy(),
            )
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            exit_code = result.returncode

            # 构建输出
            output_parts = []
            if stdout:
                output_parts.append(stdout)
            if stderr:
                output_parts.append(f"[stderr]\n{stderr}")

            combined = "\n".join(output_parts).rstrip()

            # 截断超长输出
            if len(combined) > MAX_OUTPUT:
                combined = combined[:MAX_OUTPUT] + f"\n[...输出已截断，共 {len(combined)} 字符]"

            if exit_code != 0:
                if combined:
                    return f"{combined}\n[退出码: {exit_code}]"
                return f"命令执行失败，退出码: {exit_code}"

            return combined if combined else "命令执行成功（退出码 0，无输出）"

        except subprocess.TimeoutExpired:
            return f"错误：命令执行超时（{timeout} 秒）\n命令：{command[:200]}"
        except FileNotFoundError as e:
            return f"错误：命令未找到 - {e}"
        except PermissionError as e:
            return f"错误：权限不足 - {e}"
        except Exception as e:
            return f"错误：命令执行异常 - {type(e).__name__}: {e}"

    @staticmethod
    def _check_safety(command: str) -> str:
        """检查危险命令，返回错误信息（安全则返回空字符串）。"""
        if _DANGEROUS_RE.search(command):
            return (
                f"安全检查拒绝：该命令包含潜在危险操作，已阻止执行。\n"
                f"命令：{command[:200]}\n"
                f"如果确实需要执行，请在终端手动运行。"
            )
        return ""
