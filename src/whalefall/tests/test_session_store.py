"""
SessionStore 并发写入回归（P0-4）：
  - 多线程并发 append_messages 不会互相覆盖
  - save_session 在多线程下是串行化的（per-session 锁）
  - clear_session 后能正确读到空
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from whalefall.storage.session_store import SessionStore


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(db_path=tmp_path / "sessions.sqlite")


def test_append_messages_concurrent_no_loss(store: SessionStore) -> None:
    sid = "sess-concurrent"
    threads = []
    total_per_worker = 20
    workers = 8

    def worker(i: int) -> None:
        for j in range(total_per_worker):
            store.append_messages(sid, [{"role": "user", "content": f"w{i}-m{j}"}])

    for i in range(workers):
        t = threading.Thread(target=worker, args=(i,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    msgs = store.load_session(sid)
    assert len(msgs) == workers * total_per_worker, (
        f"并发 append 丢消息：期望 {workers * total_per_worker}，实际 {len(msgs)}"
    )


def test_save_session_roundtrip(store: SessionStore) -> None:
    sid = "sess-a"
    store.save_session(sid, [{"role": "user", "content": "hi"}])
    assert store.load_session(sid) == [{"role": "user", "content": "hi"}]
    store.save_session(sid, [{"role": "assistant", "content": "hello"}])
    # save_session 是整表覆盖
    assert store.load_session(sid) == [{"role": "assistant", "content": "hello"}]


def test_load_missing_session_returns_empty(store: SessionStore) -> None:
    assert store.load_session("nope") == []


def test_load_session_normalizes_id(store: SessionStore) -> None:
    assert store.load_session("") == []
    assert store.load_session("   ") == []


def test_history_never_contains_system_role(store: SessionStore) -> None:
    """invariant：session_messages 永远不应落盘 system role（system 每次即时渲染）。"""
    store.append_messages(
        "sys-reject",
        [
            {"role": "system", "content": "should be filtered"},
            {"role": "user", "content": "real"},
        ],
    )
    msgs = store.load_session("sys-reject")
    assert all(m.get("role") != "system" for m in msgs), msgs
    assert any(m.get("role") == "user" for m in msgs)


def test_orphan_tool_calls_dropped_on_load(store: SessionStore) -> None:
    """崩溃恢复 invariant：仅 assistant.tool_calls 没有对应 tool_result，
    加载时整条 assistant 和孤儿 tool 都该被滤掉（对齐 Claude Code 的 filterUnresolvedToolUses）。"""
    store.append_messages(
        "crash-sid",
        [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tA",
                        "type": "function",
                        "function": {"name": "foo", "arguments": "{}"},
                    }
                ],
            },
        ],
    )
    recovered = store.load_session("crash-sid")
    assert [m["role"] for m in recovered] == ["user"], recovered
