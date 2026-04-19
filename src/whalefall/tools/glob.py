"""
GlobTool：文件模式匹配内建工具。

- read_only=True（只读工具，可并发）
- 参数：pattern(str), path(str 可选，默认当前目录)
- 使用 glob.glob 递归匹配，按修改时间排序
- 最多返回 500 个匹配结果
"""
from __future__ import annotations

import glob as glob_module
import os
from pathlib import Path
from typing import Any, Dict

from whalefall.tools.base import BuiltinTool, ToolContext

MAX_RESULTS = 500


class GlobTool(BuiltinTool):
    """使用 glob 模式匹配文件，按修改时间排序。"""

    name = "glob"
    description = (
        "使用 glob 模式匹配文件路径。支持 ** 递归匹配（如 **/*.py）。"
        "结果按文件修改时间排序（最新在前）。最多返回 500 个结果。"
    )
    read_only = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "glob 模式，如 '**/*.py'、'src/**/*.ts'、'*.json'",
            },
            "path": {
                "type": "string",
                "description": "搜索根目录（默认为当前工作目录）",
            },
        },
        "required": ["pattern"],
    }

    def prompt(self) -> str:
        return (
            "文件名搜索（glob）：\n"
            "- 按文件名/路径模式查找文件时使用 glob，不要用 bash find。\n"
            "- 支持 ** 递归（如 `**/*.py`、`src/**/*.ts`），结果按修改时间排序。\n"
            "- 需要按内容搜索时改用 grep。"
        )

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        pattern = args.get("pattern", "").strip()
        search_path = args.get("path", "").strip()

        if not pattern:
            return "错误：pattern 参数不能为空"

        # 确定搜索根目录
        if search_path:
            root = Path(search_path)
        else:
            root = Path.cwd()

        if not root.exists():
            return f"错误：搜索目录不存在: {root}"
        if not root.is_dir():
            return f"错误：路径不是目录: {root}"

        try:
            # 构建完整搜索模式
            if os.path.isabs(pattern):
                full_pattern = pattern
            else:
                full_pattern = str(root / pattern)

            # 执行 glob 匹配（recursive=True 支持 **）
            matches = glob_module.glob(full_pattern, recursive=True)

            if not matches:
                return (
                    f"没有匹配结果\n"
                    f"模式: {pattern}\n"
                    f"搜索目录: {root}"
                )

            # 按修改时间排序（最新在前）
            def _mtime(p: str) -> float:
                try:
                    return os.path.getmtime(p)
                except OSError:
                    return 0.0

            matches.sort(key=_mtime, reverse=True)

            # 截断到最大数量
            total = len(matches)
            truncated = False
            if total > MAX_RESULTS:
                matches = matches[:MAX_RESULTS]
                truncated = True

            # 格式化输出
            result_lines = []
            for m in matches:
                try:
                    rel = os.path.relpath(m, start=str(root))
                    result_lines.append(rel)
                except ValueError:
                    result_lines.append(m)

            output = "\n".join(result_lines)
            header = f"找到 {total} 个匹配{'（仅显示前 ' + str(MAX_RESULTS) + ' 个）' if truncated else ''}\n"
            header += f"模式: {pattern} | 目录: {root}\n"

            return header + output

        except PermissionError:
            return f"错误：无权限访问目录: {root}"
        except Exception as e:
            return f"错误：glob 匹配失败 - {type(e).__name__}: {e}"
