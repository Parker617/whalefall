"""
QueryEngine：会话编排层（Session state + AgentLoop 执行）。

职责：
- 维护 session_id -> 对话历史（user/assistant/tool/assistant.tool_calls）
- 每次 submit 时把历史注入 AgentLoop.extra_messages
- 执行成功后追加本轮完整消息增量，形成多轮上下文

非职责（仍由 AgentLoop 负责）：
- 工具调度
- 权限检查
- Context 压缩
- MCP 调用
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from whalefall.storage.session_store import SessionStore
from whalefall.storage.last_session import record_last_session
from whalefall.storage.transcripts import append_transcript
from whalefall.agent.roles import AgentConfig, get_agent
from whalefall.storage.retention import RuntimeRetention

_VERDICT_RE = re.compile(r"VERDICT:\s*(PASS|FAIL|PARTIAL)", re.IGNORECASE)
DEFAULT_VERIFY_GATE_MODE = "off"  # off | block | repair
DEFAULT_HOUSEKEEPING_EVERY = 50
MAX_VERIFY_PROMPT_CHARS = 20_000
MAX_VERIFY_REPORT_CHARS = 12_000


class QueryEngine:
    """会话引擎：多轮上下文 + 可选持久化 + Verify Gate。"""

    def __init__(
        self,
        agent_loop: Any,
        *,
        max_history_messages: int = 400,  # 完整轨迹（含 tool）下约 60~100 轮
        session_store: Optional[SessionStore] = None,
        enable_persistence: bool = True,
        verify_gate_mode: Optional[str] = None,  # off | block | repair
        retention_manager: Optional[RuntimeRetention] = None,
        housekeeping_every: int = DEFAULT_HOUSEKEEPING_EVERY,
    ):
        self._loop = agent_loop
        self._max_history_messages = max(2, int(max_history_messages))
        self._sessions: Dict[str, List[Dict[str, Any]]] = {}
        self._global_lock = threading.RLock()
        self._session_locks: Dict[str, threading.Lock] = {}
        self._store = (
            session_store
            if session_store is not None
            else (SessionStore() if enable_persistence else None)
        )
        self._retention = retention_manager or RuntimeRetention()
        self._verify_gate_mode = self._normalize_verify_gate_mode(verify_gate_mode)
        self._housekeeping_every = max(
            1,
            int(
                os.getenv("WHALEFALL_RETENTION_RUN_EVERY", str(housekeeping_every))
                or housekeeping_every
            ),
        )
        self._submit_count = 0
        self._run_housekeeping(force=True)

    # ------------------------------------------------------------------ #
    #                       Session 生命周期                               #
    # ------------------------------------------------------------------ #
    def create_session(self, session_id: str) -> None:
        sid = self._normalize_session_id(session_id)
        self._ensure_session_loaded(sid)
        with self._global_lock:
            self._sessions.setdefault(sid, [])
            self._session_locks.setdefault(sid, threading.Lock())

    def clear_session(self, session_id: str) -> None:
        sid = self._normalize_session_id(session_id)
        with self._global_lock:
            self._sessions[sid] = []
            self._session_locks.setdefault(sid, threading.Lock())
        if self._store is not None:
            self._store.clear_session(sid)

    def drop_session(self, session_id: str) -> None:
        sid = self._normalize_session_id(session_id)
        with self._global_lock:
            self._sessions.pop(sid, None)
            self._session_locks.pop(sid, None)
        if self._store is not None:
            self._store.drop_session(sid)

    def delete_sessions_older_than(self, days: int) -> int:
        """删除 N 天前的所有会话，返回删除数量。同步清理内存缓存。"""
        if self._store is None:
            return 0
        count = self._store.delete_older_than(days)
        if count > 0:
            remaining = {s["session_id"] for s in self._store.list_sessions(limit=10000)}
            with self._global_lock:
                stale = [sid for sid in self._sessions if sid not in remaining]
                for sid in stale:
                    self._sessions.pop(sid, None)
                    self._session_locks.pop(sid, None)
        return count

    def get_session_messages(self, session_id: str) -> List[Dict[str, Any]]:
        sid = self._normalize_session_id(session_id)
        self._ensure_session_loaded(sid)
        with self._global_lock:
            return list(self._sessions.get(sid, []))

    def get_session_turns(self, session_id: str) -> int:
        # 完整轨迹里包含 tool/tool_calls，turn 以 user 消息数为准
        return sum(1 for m in self.get_session_messages(session_id) if m.get("role") == "user")

    def list_sessions(self, limit: int = 20) -> List[Dict[str, Any]]:
        """列出最近 N 个已持久化的会话（按最后活跃时间降序）。"""
        if self._store is None:
            return []
        return self._store.list_sessions(limit=limit)

    def compact_session(self, session_id: str) -> int:
        """
        对当前会话执行 microcompact（同步，无需 LLM）。
        返回压缩后的消息数；若无变化或无会话则返回原消息数。
        """
        sid = self._normalize_session_id(session_id)
        self._ensure_session_loaded(sid)
        lock = self._get_session_lock(sid)
        with lock:
            history = self._sessions.get(sid, [])
            if not history:
                return 0
            try:
                from whalefall.agent.compaction import ContextManager
                compactor = ContextManager()
                compacted = compactor.microcompact(history)
            except Exception:
                return len(history)
            with self._global_lock:
                self._sessions[sid] = compacted
            if self._store is not None:
                self._store.save_session(sid, compacted)
            return len(compacted)

    def load_session_into(self, from_session_id: str, to_session_id: str) -> int:
        """
        把 from_session_id 的历史加载到 to_session_id（覆盖当前内存状态）。
        返回加载的消息数。
        """
        if self._store is None:
            return 0
        from_sid = self._normalize_session_id(from_session_id)
        messages = self._store.load_session(from_sid)
        if not messages:
            return 0
        if len(messages) > self._max_history_messages:
            messages = messages[-self._max_history_messages:]
        to_sid = self._normalize_session_id(to_session_id)
        with self._global_lock:
            self._sessions[to_sid] = messages
            self._session_locks.setdefault(to_sid, threading.Lock())
        return len(self._sessions[to_sid])

    # ------------------------------------------------------------------ #
    #                       提交查询                                       #
    # ------------------------------------------------------------------ #
    def submit(
        self,
        *,
        session_id: str,
        user_query: str,
        agent_config: AgentConfig,
        model: Optional[str] = None,
        request_id: Optional[str] = None,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool_start: Optional[Callable[[str, Dict], None]] = None,
        on_tool_end: Optional[Callable[[str, str, float], None]] = None,
        on_compaction: Optional[Callable[[int, int], None]] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> str:
        """
        运行一轮查询，并把结果合并进会话历史。

        并发语义：
        - 同一个 session 串行（加 session 级锁，防止并发写历史）
        - 不同 session 并行（锁互不影响）
        """
        sid = self._normalize_session_id(session_id)
        self.create_session(sid)
        lock = self._get_session_lock(sid)

        with lock:
            # 快照本轮之前的历史；loop 里新产出的 user/assistant/tool 会通过
            # on_message_commit 即时落盘（write-ahead，对齐 Claude Code）
            history = self.get_session_messages(sid)

            def _commit(msg: Dict[str, Any]) -> None:
                self._commit_message(sid, msg)

            if hasattr(self._loop, "run_with_messages"):
                answer, _ = self._loop.run_with_messages(
                    user_query=user_query,
                    agent_config=agent_config,
                    model=model,
                    request_id=request_id,
                    extra_messages=history,
                    on_text=on_text,
                    on_tool_start=on_tool_start,
                    on_tool_end=on_tool_end,
                    on_compaction=on_compaction,
                    abort_event=abort_event,
                    on_message_commit=_commit,
                )
            else:
                # 兼容不支持 write-ahead 的老 Loop（仅返回 final_text）：
                # 整轮完成后一次性补写 user + assistant，失去"崩了不丢"能力。
                answer = self._loop.run(
                    user_query=user_query,
                    agent_config=agent_config,
                    model=model,
                    request_id=request_id,
                    extra_messages=history,
                    on_text=on_text,
                    on_tool_start=on_tool_start,
                    on_tool_end=on_tool_end,
                    on_compaction=on_compaction,
                )
                _commit({"role": "user", "content": user_query})
                _commit({"role": "assistant", "content": answer or "（无回复）"})

            final_answer = self._apply_verify_gate(
                user_query=user_query,
                assistant_reply=answer,
                agent_config=agent_config,
                model=model,
                request_id=request_id,
                history=history,
            )
            if final_answer != answer:
                # Verify Gate 罕见路径：最终文本被改写，覆写末尾 assistant。
                self._rewrite_last_assistant(sid, final_answer)

            # 记录最近活跃会话（供 CLI --resume-last / /resume-last）。
            # 所有入口（CLI/Web/Python API）共享一份 last_session.txt。
            record_last_session(sid)

            self._submit_count += 1
            self._run_housekeeping(force=False)
            return final_answer

    # ------------------------------------------------------------------ #
    #                       内部方法                                       #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_session_id(session_id: str) -> str:
        sid = (session_id or "").strip()
        return sid or "default"

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self._global_lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _ensure_session_loaded(self, session_id: str) -> None:
        if self._store is None:
            return
        with self._global_lock:
            if session_id in self._sessions:
                return
        loaded = self._store.load_session(session_id)
        if len(loaded) > self._max_history_messages:
            loaded = loaded[-self._max_history_messages:]
        with self._global_lock:
            self._sessions[session_id] = loaded
            self._session_locks.setdefault(session_id, threading.Lock())

    def _append_messages_locked(self, *, sid: str, new_messages: List[Dict[str, Any]]) -> None:
        if not new_messages:
            return
        history = self._sessions.setdefault(sid, [])
        history.extend(new_messages)
        if len(history) > self._max_history_messages:
            self._sessions[sid] = history[-self._max_history_messages :]

    def _commit_message(self, sid: str, msg: Dict[str, Any]) -> None:
        """
        write-ahead：单条消息即时落盘 + 内存 append + 超限 FIFO 回写。

        调用路径：AgentLoop 内部每产出一条 user/assistant/tool 立刻触发。
        任一环节失败都不会中断 LLM 主流程（异常会被 AgentLoop 吞掉）。
        """
        if not isinstance(msg, dict):
            return
        with self._global_lock:
            history = self._sessions.setdefault(sid, [])
            history.append(msg)
            fifo_truncated = False
            if len(history) > self._max_history_messages:
                self._sessions[sid] = history[-self._max_history_messages :]
                fifo_truncated = True
        # 不管有没有 SessionStore，transcripts 永远全量落盘（审计 / 复盘用）。
        try:
            append_transcript(sid, msg)
        except Exception:
            pass
        if self._store is None:
            return
        try:
            if fifo_truncated:
                # 超上限：一次性重写，保证磁盘也受控（对齐内存视图）
                self._store.replace_session(sid, self._sessions[sid])
            else:
                self._store.append_message(sid, msg)
        except Exception:
            pass

    def _rewrite_last_assistant(self, sid: str, new_text: str) -> None:
        """
        Verify Gate 触发时用新文本覆盖末尾的 plain assistant（无 tool_calls 的那条）。
        找不到就 append。整条会话 replace_session 一次性落盘。
        """
        final_text = (new_text or "").strip() or "（无回复）"
        with self._global_lock:
            history = self._sessions.setdefault(sid, [])
            idx = None
            for i in range(len(history) - 1, -1, -1):
                m = history[i]
                if m.get("role") == "assistant" and not m.get("tool_calls"):
                    idx = i
                    break
            if idx is None:
                history.append({"role": "assistant", "content": final_text})
            else:
                history[idx] = {"role": "assistant", "content": final_text}
        if self._store is None:
            return
        try:
            self._store.replace_session(sid, self._sessions[sid])
        except Exception:
            pass

    @staticmethod
    def _ensure_final_assistant_message(
        *,
        messages: List[Dict[str, Any]],
        user_query: str,
        assistant_reply: str,
    ) -> List[Dict[str, Any]]:
        """
        保障持久化消息合法且以最终 assistant 文本收尾：
        - 无 user 时补 user
        - 无可替换的 assistant 时追加 assistant
        - 有 plain assistant 时替换为 verify gate 后的最终答案
        """
        result: List[Dict[str, Any]] = []
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", "")).strip().lower()
            if role not in {"user", "assistant", "tool", "system"}:
                continue
            normalized = {"role": role}
            if "content" in msg:
                normalized["content"] = str(msg.get("content", ""))
            elif role in {"user", "assistant", "tool"}:
                normalized["content"] = ""
            if role == "assistant" and isinstance(msg.get("tool_calls"), list):
                normalized["tool_calls"] = msg.get("tool_calls")
            if role == "tool" and "tool_call_id" in msg:
                normalized["tool_call_id"] = str(msg.get("tool_call_id", ""))
                if "_tool_name" in msg:
                    normalized["_tool_name"] = str(msg.get("_tool_name", ""))
                ts = msg.get("_ts")
                if isinstance(ts, (int, float)):
                    normalized["_ts"] = float(ts)
            result.append(normalized)

        if not any(m.get("role") == "user" for m in result):
            result.insert(0, {"role": "user", "content": (user_query or "").strip()})

        final_text = (assistant_reply or "").strip() or "（无回复）"
        replace_idx = None
        for i in range(len(result) - 1, -1, -1):
            msg = result[i]
            if msg.get("role") != "assistant":
                continue
            # 只替换纯文本 assistant；tool_calls 中间节点保持不动
            if not msg.get("tool_calls"):
                replace_idx = i
                break
        if replace_idx is not None:
            result[replace_idx]["content"] = final_text
        else:
            result.append({"role": "assistant", "content": final_text})
        return result

    @staticmethod
    def _normalize_verify_gate_mode(mode: Optional[str]) -> str:
        raw = (mode or os.getenv("WHALEFALL_VERIFY_GATE_MODE") or DEFAULT_VERIFY_GATE_MODE).strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return "block"
        if raw in {"off", "block", "repair"}:
            return raw
        return DEFAULT_VERIFY_GATE_MODE

    @staticmethod
    def _extract_verdict(text: str) -> str:
        m = _VERDICT_RE.search(text or "")
        if not m:
            return "PARTIAL"
        return m.group(1).upper()

    @staticmethod
    def _clip(text: str, max_chars: int) -> str:
        s = text or ""
        return s if len(s) <= max_chars else s[:max_chars] + "\n[...内容已截断...]"

    def _apply_verify_gate(
        self,
        *,
        user_query: str,
        assistant_reply: str,
        agent_config: AgentConfig,
        model: Optional[str],
        request_id: Optional[str],
        history: List[Dict[str, Any]],
    ) -> str:
        """
        Verify Gate：
        - off：不做验证
        - block：FAIL 时阻断最终输出
        - repair：FAIL 时触发一次修复回合
        """
        if self._verify_gate_mode == "off":
            return assistant_reply
        # Verify Gate 只对通用主 Agent 生效，子 Agent（explore/plan/verify/...）直接跳过
        if agent_config.name != "general":
            return assistant_reply

        verify_prompt = (
            "请独立验证下面这条回答是否满足用户需求，并检查逻辑一致性与关键事实是否可被现有信息支持。\n"
            "仅输出验证报告，并必须以以下格式结尾：\n"
            "VERDICT: PASS / FAIL / PARTIAL\n\n"
            "[用户需求]\n"
            f"{self._clip(user_query, MAX_VERIFY_PROMPT_CHARS)}\n\n"
            "[待验证回答]\n"
            f"{self._clip(assistant_reply, MAX_VERIFY_PROMPT_CHARS)}"
        )
        verify_rid = f"{(request_id or 'rid')}-verify"
        try:
            verify_report = self._loop.run(
                user_query=verify_prompt,
                agent_config=get_agent("verify"),
                model=model,
                request_id=verify_rid,
            )
        except Exception as exc:
            # 验证失败时不阻断主流程
            return assistant_reply + f"\n\n[Verify Gate 警告] 验证执行失败：{type(exc).__name__}: {exc}"

        verdict = self._extract_verdict(verify_report)
        if verdict != "FAIL":
            return assistant_reply

        verify_report_short = self._clip(verify_report, MAX_VERIFY_REPORT_CHARS)
        if self._verify_gate_mode == "repair":
            repair_prompt = (
                "你上一版回答未通过验证，请根据验证报告修正后给出最终答案。\n\n"
                "[用户需求]\n"
                f"{self._clip(user_query, MAX_VERIFY_PROMPT_CHARS)}\n\n"
                "[上一版回答]\n"
                f"{self._clip(assistant_reply, MAX_VERIFY_PROMPT_CHARS)}\n\n"
                "[验证报告]\n"
                f"{verify_report_short}\n"
            )
            try:
                return self._loop.run(
                    user_query=repair_prompt,
                    agent_config=agent_config,
                    model=model,
                    request_id=f"{(request_id or 'rid')}-repair",
                    extra_messages=history,
                )
            except Exception as exc:
                return (
                    "[Verify Gate] 验证失败且修复回合执行异常，已阻断直接输出。\n\n"
                    f"[验证报告]\n{verify_report_short}\n\n"
                    f"[修复异常]\n{type(exc).__name__}: {exc}"
                )

        # mode=block
        return (
            "[Verify Gate] 验证未通过（FAIL），已阻断直接输出。\n\n"
            f"[验证报告]\n{verify_report_short}"
        )

    def _run_housekeeping(self, *, force: bool) -> None:
        if (not force) and (self._submit_count % self._housekeeping_every != 0):
            return
        try:
            self._retention.run(session_store=self._store)
        except Exception:
            # housekeeping 失败不影响主流程
            pass
