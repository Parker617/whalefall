"""
ui/slash 回归测试（P1-10）。

验证：
  - normalize 能吃掉全角 ／ 与零宽字符
  - dispatch_common 能分发 /clear /compact /resume /init /stats
  - 非斜杠输入 / 未知命令返回 handled=False
  - /resume 冷启动模式会拒绝
"""
from __future__ import annotations

import os
import tempfile

import pytest

from whalefall.ui.slash import (
    SlashContext,
    dispatch_common,
    normalize_slash_input,
    parse_slash,
)


class FakeQE:
    def __init__(self) -> None:
        self.cleared = False
        self.compacted_to = -1
        self.loaded = (None, 0)
        self._sessions: list[dict] = []

    def clear_session(self, sid: str) -> None:
        self.cleared = True

    def compact_session(self, sid: str) -> int:
        self.compacted_to = 5
        return 5

    def list_sessions(self, limit: int = 15) -> list[dict]:
        return list(self._sessions)

    def load_session_into(self, src: str, dst: str) -> int:
        self.loaded = (src, 4)
        return 4

    def get_session_turns(self, sid: str) -> int:
        return 7


def test_normalize_full_width_and_zero_width() -> None:
    assert normalize_slash_input(" ／help\u200b ") == "/help"


def test_parse_slash_basic() -> None:
    assert parse_slash("/clear") == ("/clear", "")
    assert parse_slash("/resume abc") == ("/resume", "abc")
    assert parse_slash("plain") == ("", "plain")


def test_dispatch_clear() -> None:
    qe = FakeQE()
    r = dispatch_common("/clear", SlashContext(query_engine=qe, session_id="s1"))
    assert r.handled and r.cleared and "已清空" in r.message and qe.cleared


def test_dispatch_compact() -> None:
    qe = FakeQE()
    r = dispatch_common("/compact", SlashContext(query_engine=qe, session_id="s1"))
    assert r.handled and "5" in r.message


def test_dispatch_resume_empty() -> None:
    qe = FakeQE()
    r = dispatch_common("/resume", SlashContext(query_engine=qe, session_id="s1"))
    assert r.handled and "暂无" in r.message


def test_dispatch_resume_with_arg() -> None:
    qe = FakeQE()
    r = dispatch_common("/resume abc", SlashContext(query_engine=qe, session_id="s1"))
    assert r.handled and "已恢复" in r.message and "4" in r.message


def test_dispatch_resume_cold_start() -> None:
    qe = FakeQE()
    r = dispatch_common(
        "/resume abc",
        SlashContext(query_engine=qe, session_id="s1", strict_cold_start=True),
    )
    assert r.handled and "冷启动" in r.message


def test_dispatch_stats_with_extra() -> None:
    qe = FakeQE()
    ctx = SlashContext(
        query_engine=qe,
        session_id="s1",
        extra_stats_fn=lambda: {"模型": "mock", "轮数": 3},
    )
    r = dispatch_common("/stats", ctx)
    assert r.handled and "模型" in r.message and "mock" in r.message


def test_dispatch_init_creates_file() -> None:
    qe = FakeQE()
    with tempfile.TemporaryDirectory() as tmp:
        ctx = SlashContext(query_engine=qe, session_id="s1", cwd=tmp)
        r = dispatch_common("/init", ctx)
        assert r.handled and "已创建" in r.message
        target = os.path.join(tmp, "AGENTS.md")
        assert os.path.exists(target)
        body = open(target, encoding="utf-8").read()
        # 模板应提醒本文件不会被自动加载
        assert "不会自动" in body or "不会被自动" in body
        # 第二次应报"已存在"
        r2 = dispatch_common("/init", ctx)
        assert r2.handled and "已存在" in r2.message


def test_dispatch_unknown_returns_unhandled() -> None:
    qe = FakeQE()
    r = dispatch_common("/nope", SlashContext(query_engine=qe, session_id="s1"))
    assert not r.handled


def test_dispatch_plain_text() -> None:
    qe = FakeQE()
    r = dispatch_common("hello world", SlashContext(query_engine=qe, session_id="s1"))
    assert not r.handled
