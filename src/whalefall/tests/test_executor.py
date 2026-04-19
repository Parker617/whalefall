"""
ToolExecutor 回归测试（P0-9 死循环检测兜底）。
"""
from __future__ import annotations

import pytest

from whalefall.agent.executor import DOOM_LOOP_THRESHOLD, ToolExecutor


def _make_tc(name: str, arguments) -> dict:
    return {"id": "x", "function": {"name": name, "arguments": arguments}}


def test_doom_loop_raises_on_same_calls() -> None:
    ex = ToolExecutor()
    history = [
        [_make_tc("read", '{"file_path": "/a"}')]
        for _ in range(DOOM_LOOP_THRESHOLD)
    ]
    with pytest.raises(RuntimeError, match="死循环"):
        ex.doom_loop_check(history)


def test_doom_loop_noop_for_varying_calls() -> None:
    ex = ToolExecutor()
    history = [
        [_make_tc("read", f'{{"file_path": "/a{i}"}}')]
        for i in range(DOOM_LOOP_THRESHOLD)
    ]
    ex.doom_loop_check(history)


def test_doom_loop_noop_insufficient_history() -> None:
    ex = ToolExecutor()
    history = [[_make_tc("read", '{"file_path": "/a"}')]]
    ex.doom_loop_check(history)


def test_doom_loop_survives_bad_json_args() -> None:
    """P0-9：json 解析失败也要能继续指纹化（fallback 到 str）。"""
    ex = ToolExecutor()
    bad = "{{broken json"
    history = [[_make_tc("bash", bad)] for _ in range(DOOM_LOOP_THRESHOLD)]
    with pytest.raises(RuntimeError, match="死循环"):
        ex.doom_loop_check(history)


def test_doom_loop_non_dict_arguments() -> None:
    """arguments 可能是 dict 而非 JSON 字符串，也要处理。"""
    ex = ToolExecutor()
    history = [[_make_tc("read", {"file_path": "/a"})] for _ in range(DOOM_LOOP_THRESHOLD)]
    with pytest.raises(RuntimeError, match="死循环"):
        ex.doom_loop_check(history)
