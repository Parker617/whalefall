"""
NotebookEditTool 原子写入回归（P0-7）。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from whalefall.tools.notebook_edit import NotebookEditTool


def _empty_notebook() -> dict:
    return {
        "cells": [{"cell_type": "code", "metadata": {}, "source": [], "outputs": [], "execution_count": None}],
        "metadata": {"kernelspec": {"name": "python3"}, "language_info": {"name": "python"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def test_save_notebook_atomic_success(tmp_path: Path) -> None:
    nb_path = tmp_path / "sub" / "x.ipynb"
    NotebookEditTool._save_notebook(nb_path, _empty_notebook())
    assert nb_path.exists()
    data = json.loads(nb_path.read_text(encoding="utf-8"))
    assert data["nbformat"] == 4

    # 目录里不应残留 .x.ipynb.*.tmp 这种文件
    leftovers = list((tmp_path / "sub").glob(".x.ipynb.*.tmp"))
    assert not leftovers, f"不应有残留 tmp：{leftovers}"


def test_save_notebook_rollback_on_write_failure(tmp_path: Path) -> None:
    nb_path = tmp_path / "y.ipynb"
    original = {"nbformat": 4, "nbformat_minor": 5, "cells": [], "metadata": {}}
    NotebookEditTool._save_notebook(nb_path, original)

    # 让 os.replace 失败，模拟 rename 阶段崩溃
    with mock.patch("whalefall.tools.notebook_edit.os.replace", side_effect=OSError("boom")):
        with pytest.raises(OSError):
            NotebookEditTool._save_notebook(nb_path, {"nbformat": 4, "nbformat_minor": 5, "cells": [], "metadata": {"x": 1}})

    # 原文件不应被破坏
    data = json.loads(nb_path.read_text(encoding="utf-8"))
    assert data == original
    # tmp 文件应被清理
    leftovers = list(tmp_path.glob(".y.ipynb.*.tmp"))
    assert not leftovers, f"写失败后仍残留 tmp：{leftovers}"
