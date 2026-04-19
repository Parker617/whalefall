"""
WriteTool：写文件内建工具（完整覆盖）。

- read_only=False（写工具，串行执行）
- 参数：file_path(str), content(str)
- 自动创建父目录
- 返回：写入字节数、文件路径
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from whalefall.tools.base import BuiltinTool, ToolContext


class WriteTool(BuiltinTool):
    """将内容写入文件（完整覆盖）。自动创建父目录。"""

    name = "write"
    description = (
        "将内容写入文件（完整覆盖原有内容）。如果父目录不存在会自动创建。"
        "返回写入字节数和文件路径。"
    )
    read_only = False
    parameters_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要写入的文件路径（绝对路径或相对路径）",
            },
            "content": {
                "type": "string",
                "description": "要写入的文件内容",
            },
        },
        "required": ["file_path", "content"],
    }

    def prompt(self) -> str:
        return (
            "文件写入（write）：\n"
            "- 写入整文件（完整覆盖）时使用 write，不要用 bash echo/重定向替代。\n"
            "- 仅在确有必要创建新文件时使用；修改已有文件优先 edit。\n"
            "- 写入前确认路径和覆盖影响，避免覆盖用户未预期的文件。"
        )

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        file_path = args.get("file_path", "").strip()
        content = args.get("content", "")

        if not file_path:
            return "错误：file_path 参数不能为空"

        path = Path(file_path)

        # 安全检查：防止写入系统关键目录
        try:
            resolved = path.resolve()
            for restricted in [Path("/etc"), Path("/usr"), Path("/bin"), Path("/sbin")]:
                if str(resolved).startswith(str(restricted)):
                    return f"错误：不允许写入系统目录: {resolved}"
        except Exception:
            pass

        try:
            # 自动创建父目录
            path.parent.mkdir(parents=True, exist_ok=True)

            # 写文件（UTF-8 编码）
            encoded = content.encode("utf-8")
            path.write_bytes(encoded)

            bytes_written = len(encoded)
            lines_written = content.count("\n") + (1 if content else 0)

            return (
                f"文件写入成功\n"
                f"路径: {path.resolve()}\n"
                f"字节数: {bytes_written}\n"
                f"行数: {lines_written}"
            )

        except PermissionError:
            return f"错误：无写入权限: {file_path}"
        except IsADirectoryError:
            return f"错误：路径是目录，无法写文件: {file_path}"
        except OSError as e:
            return f"错误：文件系统错误 - {e}"
        except Exception as e:
            return f"错误：写文件失败 - {type(e).__name__}: {e}"
