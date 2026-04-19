"""
ReadTool：读取文件内容的内建工具。

- read_only=True（只读工具，可并发）
- 参数：file_path(str), offset(int 可选), limit(int 可选)
- 支持行范围（offset=行起始，limit=行数），默认最多读 2000 行
- 返回：cat -n 格式（行号 + 内容）
- 支持图片文件：返回 base64 data URI
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any, Dict, Tuple

from whalefall.tools.base import BuiltinTool, ToolContext

DEFAULT_MAX_LINES = 2_000
MAX_LINES_HARD = 10_000   # 绝对上限

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"}


class ReadTool(BuiltinTool):
    """读取文件内容，返回带行号的文本（cat -n 格式）。"""

    name = "read"
    description = (
        "读取文件内容。返回带行号的文本（cat -n 格式）。"
        "支持 offset（起始行号，1-based）和 limit（读取行数）参数。"
        "默认最多读取 2000 行。支持读取图片文件（返回 base64）。"
    )
    read_only = True
    max_result_chars = 0   # 自己处理截断，不走全局限制
    parameters_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要读取的文件路径（绝对路径或相对路径）",
            },
            "offset": {
                "type": "integer",
                "description": "起始行号（1-based），默认从第 1 行开始",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": "读取的最大行数，默认 2000 行",
                "default": 2000,
            },
        },
        "required": ["file_path"],
    }

    def prompt(self) -> str:
        return (
            "文件读取（read）：\n"
            "- 查看文件内容时使用 read，而非 bash cat/head/tail。\n"
            "- 大文件用 offset + limit 分段读取，默认最多 2000 行。\n"
            "- 支持读取图片（返回 base64），但不要把 read 用在二进制大文件上。"
        )

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        file_path = args.get("file_path", "").strip()
        offset = int(args.get("offset") or 1)
        limit = min(int(args.get("limit") or DEFAULT_MAX_LINES), MAX_LINES_HARD)

        if not file_path:
            return "错误：file_path 参数不能为空"

        path = Path(file_path)
        if not path.exists():
            return f"错误：文件不存在: {file_path}"
        if path.is_dir():
            return f"错误：路径是目录，不是文件: {file_path}"

        # 图片文件：返回 base64
        suffix = path.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            return self._read_image(path)

        # 文本文件：逐行流式读取窗口，避免整文件读入内存
        result, preview, total_chars, err = self._read_text_window(path, offset, limit)
        if err is not None:
            return f"错误：无法读取文件: {path}\n原因: {err}"

        # 记录到 recently_read（供压缩后恢复用），仅存预览内容
        try:
            entry = {"path": str(path), "content": preview, "chars": total_chars}
            ctx.recently_read = [e for e in ctx.recently_read if e["path"] != str(path)]
            ctx.recently_read.append(entry)
            ctx.recently_read = ctx.recently_read[-10:]   # 最多保留 10 个
        except Exception:
            pass

        return result

    @staticmethod
    def _read_text_window(path: Path, offset: int, limit: int) -> Tuple[str, str, int, str | None]:
        """
        逐行读取并返回:
        - cat -n 窗口输出
        - 预览内容（最多 15k）
        - 总字符数
        - 错误信息（无错误为 None）
        """
        start_line = max(1, int(offset or 1))
        line_limit = max(1, min(int(limit or DEFAULT_MAX_LINES), MAX_LINES_HARD))
        end_line = start_line + line_limit - 1

        for encoding in ("utf-8", "gbk", "latin-1"):
            try:
                return ReadTool._read_with_encoding(path, encoding, start_line, end_line)
            except UnicodeDecodeError:
                continue
            except Exception as e:
                return "", "", 0, f"{type(e).__name__}: {e}"
        return "", "", 0, "编码不支持"

    @staticmethod
    def _read_with_encoding(
        path: Path,
        encoding: str,
        start_line: int,
        end_line: int,
    ) -> Tuple[str, str, int, str | None]:
        """按指定编码逐行读取窗口内容，不缓存整文件。"""
        try:
            total_lines = 0
            total_chars = 0
            selected: list[str] = []
            preview_parts: list[str] = []
            preview_len = 0

            with path.open("r", encoding=encoding, errors="strict") as f:
                for raw_line in f:
                    total_lines += 1
                    total_chars += len(raw_line)

                    # recently_read 预览（最多 15k）
                    if preview_len < 15_000:
                        chunk = raw_line[: 15_000 - preview_len]
                        preview_parts.append(chunk)
                        preview_len += len(chunk)

                    if start_line <= total_lines <= end_line:
                        selected.append(f"{total_lines:>6}\t{raw_line.rstrip()}")

            show_start = start_line if total_lines > 0 else 0
            show_end = min(end_line, total_lines)
            meta = f"文件: {path} | 共 {total_lines} 行 | 显示第 {show_start}-{show_end} 行"
            if total_lines > show_end:
                meta += f" | 还有 {total_lines - show_end} 行未显示（调整 offset/limit 参数查看）"

            if total_lines == 0:
                body = "（文件为空）"
            elif selected:
                body = "\n".join(selected)
            else:
                body = "（范围内无内容）"

            return f"{meta}\n{body}", "".join(preview_parts), total_chars, None
        except Exception as e:
            return "", "", 0, f"{type(e).__name__}: {e}"

    def _read_image(self, path: Path) -> str:
        """读取图片文件，返回 base64 data URI。"""
        try:
            mime_type, _ = mimetypes.guess_type(str(path))
            if not mime_type:
                mime_type = "image/png"
            data = path.read_bytes()
            b64 = base64.b64encode(data).decode("ascii")
            size_kb = len(data) / 1024
            return (
                f"图片文件: {path}\n"
                f"大小: {size_kb:.1f} KB | MIME: {mime_type}\n"
                f"data:{mime_type};base64,{b64[:200]}...[base64 内容已截断]"
            )
        except Exception as e:
            return f"错误：读取图片失败 - {e}"
