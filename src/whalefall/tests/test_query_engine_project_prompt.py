"""
QueryEngine 项目提示词（Layer 3）路由测试。

验证 submit(project_prompt=...) 四种语义：
  1. 首次显式传入 → 透传给 AgentLoop 并持久化到 SessionStore
  2. 后续不传（None） → 从 SessionStore 兜底回填
  3. 显式传空串 → 清除持久化值；本轮为 None
  4. 已清除后再不传 → 仍为 None

以及 QueryEngine.get_project_prompt / set_project_prompt 便捷 API。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from whalefall.agent.query_engine import QueryEngine
from whalefall.agent.roles import get_agent, load_agents
from whalefall.storage.session_store import SessionStore


@pytest.fixture(scope="module", autouse=True)
def _loaded_agents() -> None:
    load_agents()


class FakeLoop:
    """最小 AgentLoop 替身，只记录收到的 project_prompt。"""

    def __init__(self) -> None:
        self.captured: List[Optional[str]] = []

    def run_with_messages(
        self,
        *,
        user_query: str,
        agent_config: Any,
        project_prompt: Optional[str] = None,
        **_: Any,
    ) -> tuple[str, List[Dict[str, Any]]]:
        self.captured.append(project_prompt)
        return (
            "done",
            [
                {"role": "user", "content": user_query},
                {"role": "assistant", "content": "done"},
            ],
        )


@pytest.fixture()
def engine(tmp_path: Path) -> tuple[QueryEngine, FakeLoop, SessionStore]:
    store = SessionStore(db_path=tmp_path / "qe.sqlite")
    loop = FakeLoop()
    qe = QueryEngine(loop, session_store=store, enable_persistence=False)
    # SessionStore 已被显式注入，enable_persistence=False 只是避免另建默认 store
    return qe, loop, store


def _agent():
    return get_agent("general")


def test_submit_explicit_persists_and_forwards(engine) -> None:
    qe, loop, store = engine
    qe.submit(
        session_id="s1",
        user_query="q1",
        agent_config=_agent(),
        project_prompt="# proj A\nrule",
    )
    assert loop.captured == ["# proj A\nrule"]
    assert store.load_project_prompt("s1") == "# proj A\nrule"


def test_submit_none_falls_back_to_store(engine) -> None:
    qe, loop, store = engine
    store.save_project_prompt("s2", "# persisted")
    qe.submit(session_id="s2", user_query="q", agent_config=_agent())
    assert loop.captured[-1] == "# persisted"


def test_submit_empty_string_clears(engine) -> None:
    qe, loop, store = engine
    store.save_project_prompt("s3", "# to-remove")
    qe.submit(
        session_id="s3",
        user_query="q",
        agent_config=_agent(),
        project_prompt="",
    )
    assert loop.captured[-1] is None
    assert store.load_project_prompt("s3") is None


def test_submit_after_clear_stays_none(engine) -> None:
    qe, loop, _ = engine
    qe.submit(session_id="s4", user_query="q1", agent_config=_agent(), project_prompt="x")
    qe.submit(session_id="s4", user_query="q2", agent_config=_agent(), project_prompt="")
    qe.submit(session_id="s4", user_query="q3", agent_config=_agent())
    assert loop.captured[-3:] == ["x", None, None]


def test_get_and_set_project_prompt_api(engine) -> None:
    qe, _, _ = engine
    assert qe.get_project_prompt("brand-new") is None
    qe.set_project_prompt("brand-new", "# hello")
    assert qe.get_project_prompt("brand-new") == "# hello"
    qe.set_project_prompt("brand-new", None)
    assert qe.get_project_prompt("brand-new") is None
    qe.set_project_prompt("brand-new", "   ")
    assert qe.get_project_prompt("brand-new") is None


def test_clear_session_preserves_project_prompt(engine) -> None:
    """clear_session 只清消息历史，不应动项目提示词。"""
    qe, _, store = engine
    qe.submit(
        session_id="s5",
        user_query="q",
        agent_config=_agent(),
        project_prompt="# keep",
    )
    qe.clear_session("s5")
    assert store.load_project_prompt("s5") == "# keep"


def test_drop_session_clears_project_prompt(engine) -> None:
    qe, _, store = engine
    qe.submit(
        session_id="s6",
        user_query="q",
        agent_config=_agent(),
        project_prompt="# gone",
    )
    qe.drop_session("s6")
    assert store.load_project_prompt("s6") is None
