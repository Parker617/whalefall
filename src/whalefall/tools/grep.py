"""
GrepTool：内容搜索内建工具。

- read_only=True（只读工具，可并发）
- 参数：pattern(str), path(str 可选), glob(str 可选), output_mode(str)
- output_mode：files_with_matches / content / count
- 优先用 ripgrep（rg），fallback 到 Python re
- 支持：-i（忽略大小写）、context lines（-C）、文件类型过滤
- 返回匹配结果，超过 1000 行截断
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from whalefall.tools.base import BuiltinTool, ToolContext

MAX_OUTPUT_LINES = 1_000
MAX_OUTPUT_CHARS = 50_000


class GrepTool(BuiltinTool):
    """搜索文件内容，支持正则表达式。优先使用 ripgrep（rg）。"""

    name = "grep"
    description = (
        "在文件中搜索匹配正则表达式的内容。优先使用 ripgrep（rg），fallback 到 Python re。\n"
        "output_mode 可选：\n"
        "  - files_with_matches（默认）：仅返回匹配的文件路径\n"
        "  - content：返回匹配行内容（含行号）\n"
        "  - count：返回每个文件的匹配次数\n"
        "结果超过 1000 行时自动截断。"
    )
    read_only = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "正则表达式搜索模式",
            },
            "path": {
                "type": "string",
                "description": "搜索根目录或文件路径（默认当前目录）",
            },
            "glob": {
                "type": "string",
                "description": "文件过滤 glob 模式，如 '*.py'、'**/*.ts'",
            },
            "output_mode": {
                "type": "string",
                "enum": ["files_with_matches", "content", "count"],
                "description": "输出模式：files_with_matches（默认）/ content / count",
                "default": "files_with_matches",
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "是否忽略大小写（默认 false）",
                "default": False,
            },
            "context": {
                "type": "integer",
                "description": "显示匹配行前后各 N 行（content 模式有效，默认 0）",
                "default": 0,
            },
        },
        "required": ["pattern"],
    }

    def prompt(self) -> str:
        return (
            "内容搜索（grep）：\n"
            "- 在代码/文本里搜关键字、符号、正则时使用 grep，不要用 bash grep/rg 代替。\n"
            "- 合理选 output_mode：files_with_matches（找文件）/ content（看上下文）/ count（统计）。\n"
            "- 配合 glob 参数限定文件范围，避免扫描无关目录。"
        )

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        pattern = args.get("pattern", "").strip()
        search_path = args.get("path", "").strip() or str(Path.cwd())
        glob_pattern = args.get("glob", "").strip()
        output_mode = args.get("output_mode", "files_with_matches")
        case_insensitive = bool(args.get("case_insensitive", False))
        context_lines = int(args.get("context", 0))

        if not pattern:
            return "错误：pattern 参数不能为空"

        # 优先尝试 ripgrep
        rg_result = self._try_ripgrep(
            pattern, search_path, glob_pattern, output_mode,
            case_insensitive, context_lines,
        )
        if rg_result is not None:
            return rg_result

        # fallback 到 Python re
        return self._python_grep(
            pattern, search_path, glob_pattern, output_mode,
            case_insensitive, context_lines,
        )

    # ------------------------------------------------------------------ #
    #                       ripgrep 实现                                   #
    # ------------------------------------------------------------------ #
    def _try_ripgrep(
        self,
        pattern: str,
        path: str,
        glob: str,
        output_mode: str,
        case_insensitive: bool,
        context: int,
    ) -> Optional[str]:
        """尝试用 ripgrep 搜索，返回 None 表示 rg 不可用。"""
        try:
            # 检查 rg 是否可用
            subprocess.run(
                ["rg", "--version"],
                capture_output=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None  # rg 不可用

        cmd = ["rg", "--no-heading"]

        if case_insensitive:
            cmd.append("-i")

        if output_mode == "files_with_matches":
            cmd.append("-l")
        elif output_mode == "count":
            cmd.append("-c")
        elif output_mode == "content":
            cmd.extend(["--line-number"])
            if context > 0:
                cmd.extend(["-C", str(context)])

        if glob:
            cmd.extend(["--glob", glob])

        cmd.extend(["--", pattern, path])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout or ""
            stderr = result.stderr or ""

            # rg 退出码：0=有匹配, 1=无匹配, 2=错误
            if result.returncode == 2:
                return f"搜索错误（ripgrep）: {stderr.strip()}"

            if not output.strip():
                return f"没有匹配结果\n模式: {pattern}\n路径: {path}"

            return self._truncate_output(output, output_mode, pattern, path)

        except subprocess.TimeoutExpired:
            return "错误：搜索超时（30 秒）"
        except Exception:
            return None  # 降级到 Python re

    # ------------------------------------------------------------------ #
    #                       Python re 实现                                 #
    # ------------------------------------------------------------------ #
    def _python_grep(
        self,
        pattern: str,
        path: str,
        glob_pattern: str,
        output_mode: str,
        case_insensitive: bool,
        context: int,
    ) -> str:
        """Python re 实现的 grep，fallback 用。"""
        import glob as glob_module

        flags = re.IGNORECASE if case_insensitive else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error as e:
            return f"错误：无效的正则表达式 - {e}"

        # 收集要搜索的文件
        search_root = Path(path)
        if search_root.is_file():
            files = [search_root]
        else:
            if glob_pattern:
                files = [Path(p) for p in glob_module.glob(str(search_root / glob_pattern), recursive=True)]
                files = [f for f in files if f.is_file()]
            else:
                files = list(search_root.rglob("*"))
                files = [f for f in files if f.is_file()]

        if not files:
            return f"没有找到要搜索的文件\n路径: {path}"

        results: List[str] = []
        total_count = 0

        for file_path in sorted(files):
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except (PermissionError, IsADirectoryError, OSError):
                continue

            lines = content.splitlines()
            matches = [(i + 1, line) for i, line in enumerate(lines) if compiled.search(line)]

            if not matches:
                continue

            rel_path = str(file_path)
            try:
                rel_path = str(file_path.relative_to(search_root))
            except ValueError:
                pass

            if output_mode == "files_with_matches":
                results.append(rel_path)
            elif output_mode == "count":
                results.append(f"{rel_path}: {len(matches)}")
            elif output_mode == "content":
                for lineno, line in matches:
                    results.append(f"{rel_path}:{lineno}: {line}")

            total_count += len(matches)

        if not results:
            return f"没有匹配结果\n模式: {pattern}\n路径: {path}"

        output = "\n".join(results)
        return self._truncate_output(output, output_mode, pattern, path)

    # ------------------------------------------------------------------ #
    #                       工具方法                                       #
    # ------------------------------------------------------------------ #
    def _truncate_output(self, output: str, output_mode: str, pattern: str, path: str) -> str:
        """截断超长输出。"""
        lines = output.splitlines()
        total_lines = len(lines)

        truncated = False
        if total_lines > MAX_OUTPUT_LINES:
            lines = lines[:MAX_OUTPUT_LINES]
            truncated = True

        result = "\n".join(lines)
        if len(result) > MAX_OUTPUT_CHARS:
            result = result[:MAX_OUTPUT_CHARS]
            truncated = True

        header = f"搜索: {pattern!r} | 路径: {path} | 模式: {output_mode}\n"
        if truncated:
            header += f"（共 {total_lines} 行结果，仅显示前 {MAX_OUTPUT_LINES} 行）\n"

        return header + result
