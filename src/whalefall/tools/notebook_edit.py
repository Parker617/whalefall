"""
NotebookEditTool：编辑 .ipynb 文件的内建工具。

支持：
- inspect           查看 notebook 结构摘要
- replace_cell      替换指定 cell 内容
- append_cell       末尾追加 cell
- insert_cell       指定位置插入 cell
- delete_cell       删除指定 cell
- clear_outputs     清空 code cell 输出
- set_cell_metadata 设置 cell metadata
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from whalefall.tools.base import BuiltinTool, ToolContext


def _to_source_list(source: str) -> List[str]:
    if source == "":
        return []
    lines = source.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = lines[-1] + "\n"
    return lines


def _cell_preview(cell: Dict[str, Any], max_chars: int = 70) -> str:
    src = "".join(cell.get("source") or [])
    src = src.replace("\n", " ").strip()
    if len(src) > max_chars:
        return src[:max_chars] + "..."
    return src


class NotebookEditTool(BuiltinTool):
    """对 Jupyter Notebook 进行结构化编辑。"""

    name = "notebook_edit"
    description = (
        "编辑 Jupyter Notebook（.ipynb）。支持 inspect/replace_cell/append_cell/"
        "insert_cell/delete_cell/clear_outputs/set_cell_metadata。"
    )
    read_only = False
    max_result_chars = 20_000
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "notebook 文件路径（.ipynb）",
            },
            "action": {
                "type": "string",
                "enum": [
                    "inspect",
                    "replace_cell",
                    "append_cell",
                    "insert_cell",
                    "delete_cell",
                    "clear_outputs",
                    "set_cell_metadata",
                ],
                "description": "要执行的动作",
            },
            "cell_index": {
                "type": "integer",
                "description": "目标 cell 下标（从 0 开始）",
            },
            "cell_type": {
                "type": "string",
                "enum": ["code", "markdown", "raw"],
                "description": "append/insert 时的 cell 类型，默认 code",
                "default": "code",
            },
            "source": {
                "type": "string",
                "description": "replace/append/insert 时写入的文本内容",
            },
            "metadata": {
                "type": "object",
                "description": "set_cell_metadata 时要写入的 metadata 对象",
            },
            "execution_count": {
                "type": ["integer", "null"],
                "description": "code cell 的 execution_count（可选）",
            },
            "all_code_cells": {
                "type": "boolean",
                "description": "clear_outputs 时是否清空全部 code cell，默认 false",
                "default": False,
            },
        },
        "required": ["path", "action"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        path_str = str(args.get("path", "")).strip()
        action = str(args.get("action", "")).strip().lower()

        if not path_str:
            return "错误：path 参数不能为空"
        if not action:
            return "错误：action 参数不能为空"

        path = Path(path_str).expanduser().resolve()
        if path.suffix.lower() != ".ipynb":
            return f"错误：仅支持 .ipynb 文件: {path}"

        nb = self._load_or_init_notebook(path, action)
        if isinstance(nb, str):
            return nb  # 错误信息

        cells = nb.setdefault("cells", [])
        if not isinstance(cells, list):
            return "错误：notebook 格式非法（cells 不是数组）"

        try:
            if action == "inspect":
                return self._inspect(path, nb)
            if action == "replace_cell":
                result = self._replace_cell(cells, args)
                if isinstance(result, str):
                    return result
            elif action == "append_cell":
                result = self._append_cell(cells, args)
                if isinstance(result, str):
                    return result
            elif action == "insert_cell":
                result = self._insert_cell(cells, args)
                if isinstance(result, str):
                    return result
            elif action == "delete_cell":
                result = self._delete_cell(cells, args)
                if isinstance(result, str):
                    return result
            elif action == "clear_outputs":
                result = self._clear_outputs(cells, args)
                if isinstance(result, str):
                    return result
            elif action == "set_cell_metadata":
                result = self._set_cell_metadata(cells, args)
                if isinstance(result, str):
                    return result
            else:
                return f"错误：不支持的 action={action}"

            self._save_notebook(path, nb)
            return (
                f"notebook 已更新\n"
                f"路径: {path}\n"
                f"动作: {action}\n"
                f"cell 数量: {len(cells)}"
            )
        except Exception as exc:
            return f"错误：notebook_edit 失败 - {type(exc).__name__}: {exc}"

    def _load_or_init_notebook(self, path: Path, action: str) -> Dict[str, Any] | str:
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                nb = json.loads(text)
                if not isinstance(nb, dict):
                    return "错误：notebook 根节点不是对象"
                nb.setdefault("nbformat", 4)
                nb.setdefault("nbformat_minor", 5)
                nb.setdefault("metadata", {})
                nb.setdefault("cells", [])
                return nb
            except Exception as exc:
                return f"错误：读取 notebook 失败 - {type(exc).__name__}: {exc}"

        # 不存在时只允许创建型动作
        if action in {"append_cell", "insert_cell"}:
            return {
                "nbformat": 4,
                "nbformat_minor": 5,
                "metadata": {},
                "cells": [],
            }
        return f"错误：notebook 不存在: {path}"

    @staticmethod
    def _save_notebook(path: Path, nb: Dict[str, Any]) -> None:
        """原子写入：先写到同目录 tmp 文件，再原子 rename 覆盖。

        避免进程被 kill/异常退出时留下半写文件导致 notebook 损坏。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(nb, ensure_ascii=False, indent=1) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(str(tmp_path), str(path))
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise

    @staticmethod
    def _parse_index(args: Dict[str, Any], cells_len: int, *, allow_end: bool = False) -> int | str:
        if "cell_index" not in args:
            return "错误：cell_index 参数缺失"
        try:
            idx = int(args.get("cell_index"))
        except Exception:
            return "错误：cell_index 必须是整数"
        hi = cells_len if allow_end else cells_len - 1
        if idx < 0 or idx > hi:
            return f"错误：cell_index 越界（当前范围 0..{hi}）"
        return idx

    @staticmethod
    def _make_cell(cell_type: str, source: str, execution_count: Any = None) -> Dict[str, Any]:
        if cell_type not in {"code", "markdown", "raw"}:
            cell_type = "code"
        cell: Dict[str, Any] = {
            "cell_type": cell_type,
            "metadata": {},
            "source": _to_source_list(source),
        }
        if cell_type == "code":
            cell["execution_count"] = execution_count if execution_count is not None else None
            cell["outputs"] = []
        return cell

    def _inspect(self, path: Path, nb: Dict[str, Any]) -> str:
        cells = nb.get("cells") or []
        lines = [
            f"路径: {path}",
            f"nbformat: {nb.get('nbformat')}.{nb.get('nbformat_minor')}",
            f"cells: {len(cells)}",
            "",
            "Cell 列表：",
        ]
        for i, cell in enumerate(cells):
            ctype = str(cell.get("cell_type", "unknown"))
            src = "".join(cell.get("source") or [])
            lines.append(
                f"[{i}] {ctype:<8} chars={len(src):<6} preview={_cell_preview(cell)}"
            )
        return "\n".join(lines)

    def _replace_cell(self, cells: List[Dict[str, Any]], args: Dict[str, Any]) -> str | None:
        idx = self._parse_index(args, len(cells))
        if isinstance(idx, str):
            return idx
        source = str(args.get("source", ""))
        cell = cells[idx]
        cell["source"] = _to_source_list(source)
        if cell.get("cell_type") == "code":
            cell["execution_count"] = args.get("execution_count", None)
        return None

    def _append_cell(self, cells: List[Dict[str, Any]], args: Dict[str, Any]) -> str | None:
        cell_type = str(args.get("cell_type", "code")).strip().lower()
        source = str(args.get("source", ""))
        execution_count = args.get("execution_count", None)
        cells.append(self._make_cell(cell_type, source, execution_count))
        return None

    def _insert_cell(self, cells: List[Dict[str, Any]], args: Dict[str, Any]) -> str | None:
        idx = self._parse_index(args, len(cells), allow_end=True)
        if isinstance(idx, str):
            return idx
        cell_type = str(args.get("cell_type", "code")).strip().lower()
        source = str(args.get("source", ""))
        execution_count = args.get("execution_count", None)
        cells.insert(idx, self._make_cell(cell_type, source, execution_count))
        return None

    def _delete_cell(self, cells: List[Dict[str, Any]], args: Dict[str, Any]) -> str | None:
        idx = self._parse_index(args, len(cells))
        if isinstance(idx, str):
            return idx
        cells.pop(idx)
        return None

    def _clear_outputs(self, cells: List[Dict[str, Any]], args: Dict[str, Any]) -> str | None:
        all_code = bool(args.get("all_code_cells", False))
        if all_code:
            for cell in cells:
                if cell.get("cell_type") == "code":
                    cell["outputs"] = []
                    cell["execution_count"] = None
            return None

        idx = self._parse_index(args, len(cells))
        if isinstance(idx, str):
            return idx
        cell = cells[idx]
        if cell.get("cell_type") != "code":
            return f"错误：cell[{idx}] 不是 code cell，无法 clear_outputs"
        cell["outputs"] = []
        cell["execution_count"] = None
        return None

    def _set_cell_metadata(self, cells: List[Dict[str, Any]], args: Dict[str, Any]) -> str | None:
        idx = self._parse_index(args, len(cells))
        if isinstance(idx, str):
            return idx
        metadata = args.get("metadata")
        if not isinstance(metadata, dict):
            return "错误：metadata 必须是对象"
        cells[idx]["metadata"] = metadata
        return None

    def prompt(self) -> str:
        return (
            "Jupyter 编辑（notebook_edit）：\n"
            "- 修改 .ipynb 时优先使用 notebook_edit，避免把 notebook 当纯文本硬改。\n"
            "- 先 inspect 查看 cell 结构，再按 index 精确修改。"
        )

