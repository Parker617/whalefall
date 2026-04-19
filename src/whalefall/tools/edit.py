"""
EditTool：精确字符串替换内建工具。

- read_only=False（写工具，串行执行）
- 参数：file_path(str), old_string(str), new_string(str), replace_all(bool=False)
- 验证：old_string 不在文件中 → 给出明确错误
- 验证：replace_all=False 时 old_string 多处匹配 → 给出明确错误
- 支持 replace_all=True：替换所有匹配
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from whalefall.tools.base import BuiltinTool, ToolContext


class EditTool(BuiltinTool):
    """精确字符串替换：将文件中的 old_string 替换为 new_string。"""

    name = "edit"
    description = (
        "对文件执行精确字符串替换。将 old_string 替换为 new_string。\n"
        "replace_all=False（默认）时，old_string 必须在文件中唯一出现，否则报错。\n"
        "replace_all=True 时，替换所有出现的 old_string。\n"
        "old_string 不存在时报错，请先用 read 工具确认文件内容。"
    )
    read_only = False
    parameters_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要编辑的文件路径",
            },
            "old_string": {
                "type": "string",
                "description": "要被替换的原始字符串（必须精确匹配，包括空格和换行）",
            },
            "new_string": {
                "type": "string",
                "description": "替换后的新字符串",
            },
            "replace_all": {
                "type": "boolean",
                "description": "是否替换所有匹配（默认 false，只替换第一个且要求唯一）",
                "default": False,
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    }

    def prompt(self) -> str:
        return (
            "文件精确编辑（edit）：\n"
            "- 修改已有文件时优先使用 edit，而不是 bash sed/awk 或整文件重写。\n"
            "- 先 read 确认要替换的片段，old_string 必须在文件中唯一出现（除非 replace_all=True）。\n"
            "- old_string / new_string 请包含足够上下文，保留缩进和换行符。"
        )

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        file_path = args.get("file_path", "").strip()
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        replace_all = bool(args.get("replace_all", False))

        if not file_path:
            return "错误：file_path 参数不能为空"
        if old_string == new_string:
            return "错误：old_string 与 new_string 完全相同，无需修改"

        path = Path(file_path)
        if not path.exists():
            return f"错误：文件不存在: {file_path}"
        if path.is_dir():
            return f"错误：路径是目录，不是文件: {file_path}"

        try:
            # 读取文件（尝试多种编码）
            content = None
            encoding_used = "utf-8"
            for enc in ("utf-8", "gbk", "latin-1"):
                try:
                    content = path.read_text(encoding=enc)
                    encoding_used = enc
                    break
                except Exception:
                    continue

            if content is None:
                return f"错误：无法读取文件（编码问题）: {file_path}"

            # 检查 old_string 是否存在
            count = content.count(old_string)
            if count == 0:
                # 给出有用的提示（展示文件前 500 字符）
                preview = content[:500].replace("\n", "\\n")
                return (
                    f"错误：old_string 在文件中未找到。\n"
                    f"文件: {file_path}\n"
                    f"old_string（前200字符）: {repr(old_string[:200])}\n"
                    f"文件前500字符预览: {preview}"
                )

            # replace_all=False 时要求唯一
            if not replace_all and count > 1:
                # 显示所有匹配的行号
                lines = content.splitlines()
                match_lines = []
                search_lines = old_string.splitlines()
                first_search_line = search_lines[0] if search_lines else old_string[:50]
                for i, line in enumerate(lines):
                    if first_search_line in line:
                        match_lines.append(f"  第 {i+1} 行: {line[:100]}")

                return (
                    f"错误：old_string 在文件中出现了 {count} 次（要求唯一，或使用 replace_all=true）。\n"
                    f"文件: {file_path}\n"
                    f"包含匹配内容的行:\n" + "\n".join(match_lines[:10])
                )

            # 执行替换
            if replace_all:
                new_content = content.replace(old_string, new_string)
                replaced_count = count
            else:
                new_content = content.replace(old_string, new_string, 1)
                replaced_count = 1

            # 写回文件
            path.write_text(new_content, encoding=encoding_used)

            # 计算变更统计
            old_lines = len(content.splitlines())
            new_lines = len(new_content.splitlines())
            delta_lines = new_lines - old_lines

            return (
                f"文件编辑成功\n"
                f"路径: {path.resolve()}\n"
                f"替换次数: {replaced_count}\n"
                f"行数变化: {old_lines} → {new_lines}"
                + (f" ({delta_lines:+d})" if delta_lines != 0 else "")
            )

        except PermissionError:
            return f"错误：无权限读写文件: {file_path}"
        except OSError as e:
            return f"错误：文件系统错误 - {e}"
        except Exception as e:
            return f"错误：编辑失败 - {type(e).__name__}: {e}"
