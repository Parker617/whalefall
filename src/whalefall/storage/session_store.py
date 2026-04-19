# coding: utf-8
"""
SessionStore：会话历史持久化（SQLite）。

- 跨进程会话续跑能力
- Schema：session_id + messages_json + project_prompt + updated_at
  （project_prompt 用于持久化 Layer 3 项目提示词，Web UI 刷新/CLI 重连后能复用）
- 容量治理：TTL / 最大会话数 / 每会话消息上限 / DB 体积上限
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from whalefall.core.log import get_logger
from whalefall.core.runtime import sessions_db_path

_logger = get_logger("whalefall.session_store")

DEFAULT_MAX_SESSIONS = 200
DEFAULT_MAX_MESSAGES_PER_SESSION = 400
DEFAULT_TTL_DAYS = 30
DEFAULT_MAX_DB_BYTES = 200 * 1024 * 1024  # 200MB


def _now_ts() -> int:
    return int(time.time())


class SessionStore:
    """SQLite 会话存储（线程安全）。"""

    def __init__(self, db_path: Optional[str | Path] = None):
        self.path = (
            Path(db_path).expanduser().resolve() if db_path else sessions_db_path()
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Per-session 细粒度锁：避免同一会话在并发 WebSocket / CLI 里互相覆盖。
        self._session_locks: Dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()
        self._ensure_schema()

    def _session_lock(self, sid: str) -> threading.Lock:
        """拿到某个 session 的内存级锁。同一 sid 的 save/clear/drop 串行化。"""
        with self._session_locks_guard:
            lk = self._session_locks.get(sid)
            if lk is None:
                lk = threading.Lock()
                self._session_locks[sid] = lk
            return lk

    # ------------------------------------------------------------------ #
    #                          公共接口                                   #
    # ------------------------------------------------------------------ #

    def load_session(self, session_id: str) -> List[Dict[str, Any]]:
        sid = (session_id or "").strip()
        if not sid:
            return []
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT messages_json FROM sessions WHERE session_id = ?", (sid,)
                ).fetchone()
                return self._decode_messages(row[0]) if row else []
            finally:
                conn.close()

    def save_session(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        sid = (session_id or "").strip()
        if not sid:
            return
        payload = self._encode_messages(messages)
        now = _now_ts()
        with self._session_lock(sid):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                # 保留 project_prompt：仅更新 messages_json 和 updated_at
                conn.execute(
                    """
                    INSERT INTO sessions(session_id, messages_json, updated_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(session_id)
                    DO UPDATE SET messages_json=excluded.messages_json, updated_at=excluded.updated_at
                    """,
                    (sid, payload, now),
                )
                conn.commit()
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                _logger.warning("session save failed | sid=%s err=%s", sid, exc)
                raise
            finally:
                conn.close()

    def append_messages(
        self,
        session_id: str,
        new_messages: List[Dict[str, Any]],
    ) -> int:
        """
        原子追加若干消息到指定 session，返回追加后的总长度。

        读-改-写全程持有 per-session 锁 + BEGIN IMMEDIATE 事务，防止
        并发调用互相覆盖（此前 save_session 整表覆盖会丢消息）。
        """
        sid = (session_id or "").strip()
        if not sid or not new_messages:
            return 0
        now = _now_ts()
        with self._session_lock(sid):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT messages_json FROM sessions WHERE session_id = ?", (sid,)
                ).fetchone()
                existing = self._decode_messages(row[0]) if row else []
                merged = existing + list(new_messages)
                payload = self._encode_messages(merged)
                conn.execute(
                    """
                    INSERT INTO sessions(session_id, messages_json, updated_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(session_id)
                    DO UPDATE SET messages_json=excluded.messages_json, updated_at=excluded.updated_at
                    """,
                    (sid, payload, now),
                )
                conn.commit()
                return len(merged)
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                _logger.warning("session append failed | sid=%s err=%s", sid, exc)
                raise
            finally:
                conn.close()

    def clear_session(self, session_id: str) -> None:
        self.save_session(session_id, [])

    # ------------------------------------------------------------------ #
    #                   项目提示词（Layer 3）持久化                         #
    # ------------------------------------------------------------------ #
    def load_project_prompt(self, session_id: str) -> Optional[str]:
        """取该会话上次保存的项目提示词；无记录或空串返回 None。"""
        sid = (session_id or "").strip()
        if not sid:
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT project_prompt FROM sessions WHERE session_id = ?", (sid,)
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        value = row[0]
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def save_project_prompt(self, session_id: str, project_prompt: Optional[str]) -> None:
        """
        保存会话的项目提示词。
          - 非空串 → 写入；
          - None / 空白 → 清空该列（NULL），等同"取消本 session 的项目提示词"。
        本方法不触碰 messages_json；若 session 不存在则自动 upsert 空消息行。
        """
        sid = (session_id or "").strip()
        if not sid:
            return
        normalized: Optional[str] = None
        if project_prompt is not None:
            stripped = str(project_prompt).strip()
            normalized = stripped or None
        now = _now_ts()
        with self._session_lock(sid):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO sessions(session_id, messages_json, project_prompt, updated_at)
                    VALUES(?, '[]', ?, ?)
                    ON CONFLICT(session_id)
                    DO UPDATE SET project_prompt=excluded.project_prompt, updated_at=excluded.updated_at
                    """,
                    (sid, normalized, now),
                )
                conn.commit()
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                _logger.warning("session save_project_prompt failed | sid=%s err=%s", sid, exc)
                raise
            finally:
                conn.close()

    def delete_older_than(self, days: int) -> int:
        """删除 N 天前更新的所有会话，返回删除数量。"""
        cutoff = _now_ts() - max(1, int(days)) * 86400
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
                conn.commit()
                return int(cur.rowcount or 0)
            finally:
                conn.close()

    def drop_session(self, session_id: str) -> None:
        sid = (session_id or "").strip()
        if not sid:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
                conn.commit()
            finally:
                conn.close()

    def list_sessions(self, limit: int = 20) -> List[Dict[str, Any]]:
        """返回最近 N 个会话的元数据（id, message_count, updated_at）。"""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT session_id, messages_json, updated_at "
                    "FROM sessions ORDER BY updated_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
                result = []
                for sid, raw, ts in rows:
                    msgs = self._decode_messages(raw)
                    turns = sum(1 for m in msgs if m.get("role") == "user")
                    first_user = next(
                        (m.get("content", "") for m in msgs if m.get("role") == "user"), ""
                    )
                    preview = (first_user or "")[:50].strip().replace("\n", " ")
                    result.append({"session_id": sid, "turns": turns, "updated_at": ts, "preview": preview})
                return result
            finally:
                conn.close()

    def enforce_limits(
        self,
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_messages_per_session: int = DEFAULT_MAX_MESSAGES_PER_SESSION,
        ttl_days: int = DEFAULT_TTL_DAYS,
        max_db_bytes: int = DEFAULT_MAX_DB_BYTES,
    ) -> Dict[str, int]:
        """执行容量治理，返回统计。"""
        stats = {
            "deleted_ttl": 0,
            "trimmed_sessions": 0,
            "deleted_overflow_sessions": 0,
            "deleted_for_size": 0,
        }
        now = _now_ts()
        ttl_cutoff = now - max(1, int(ttl_days)) * 86400
        max_sessions = max(1, int(max_sessions))
        max_messages_per_session = max(2, int(max_messages_per_session))
        max_db_bytes = max(10 * 1024 * 1024, int(max_db_bytes))

        with self._lock:
            conn = self._connect()
            try:
                # TTL 清理
                cur = conn.execute("DELETE FROM sessions WHERE updated_at < ?", (ttl_cutoff,))
                stats["deleted_ttl"] = int(cur.rowcount or 0)

                # 每会话消息数裁剪
                for sid, raw in conn.execute(
                    "SELECT session_id, messages_json FROM sessions"
                ).fetchall():
                    msgs = self._decode_messages(raw)
                    if len(msgs) > max_messages_per_session:
                        trimmed = msgs[-max_messages_per_session:]
                        conn.execute(
                            "UPDATE sessions SET messages_json=?, updated_at=? WHERE session_id=?",
                            (self._encode_messages(trimmed), now, sid),
                        )
                        stats["trimmed_sessions"] += 1

                # 会话数量上限
                keep_ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT session_id FROM sessions ORDER BY updated_at DESC LIMIT ?",
                        (max_sessions,),
                    ).fetchall()
                ]
                if keep_ids:
                    placeholders = ",".join("?" for _ in keep_ids)
                    cur = conn.execute(
                        f"DELETE FROM sessions WHERE session_id NOT IN ({placeholders})",
                        keep_ids,
                    )
                else:
                    cur = conn.execute("DELETE FROM sessions")
                stats["deleted_overflow_sessions"] = int(cur.rowcount or 0)
                conn.commit()

                # DB 大小上限：按最旧会话分批删除
                current_size = self.path.stat().st_size if self.path.exists() else 0
                while current_size > max_db_bytes:
                    cur = conn.execute(
                        "DELETE FROM sessions WHERE session_id IN "
                        "(SELECT session_id FROM sessions ORDER BY updated_at ASC LIMIT 10)"
                    )
                    if not cur.rowcount:
                        break
                    stats["deleted_for_size"] += int(cur.rowcount)
                    conn.commit()
                    current_size = self.path.stat().st_size if self.path.exists() else 0

                if sum(stats.values()) > 0:
                    conn.execute("VACUUM")
                    conn.commit()
            finally:
                conn.close()

        return stats

    # ------------------------------------------------------------------ #
    #                          内部工具                                   #
    # ------------------------------------------------------------------ #

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id     TEXT PRIMARY KEY,
                        messages_json  TEXT NOT NULL,
                        project_prompt TEXT,
                        updated_at     INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at)"
                )
                # 老库迁移：已有表但无 project_prompt 列时补上
                existing_cols = {
                    row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
                }
                if "project_prompt" not in existing_cols:
                    conn.execute("ALTER TABLE sessions ADD COLUMN project_prompt TEXT")
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _decode_messages(raw: Any) -> List[Dict[str, Any]]:
        try:
            arr = json.loads(raw or "[]")
        except Exception:
            return []
        if not isinstance(arr, list):
            return []

        out: List[Dict[str, Any]] = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip().lower()
            if role not in {"system", "user", "assistant", "tool"}:
                continue
            msg: Dict[str, Any] = {"role": role}
            msg["content"] = str(item.get("content", ""))

            if role == "assistant" and isinstance(item.get("tool_calls"), list):
                tc_out = []
                for tc in item["tool_calls"]:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    tc_out.append({
                        "id": str(tc.get("id", "")),
                        "type": str(tc.get("type", "function") or "function"),
                        "function": {
                            "name": str(fn.get("name", "")),
                            "arguments": (
                                fn.get("arguments")
                                if isinstance(fn.get("arguments"), str)
                                else json.dumps(fn.get("arguments", {}), ensure_ascii=False)
                            ),
                        },
                    })
                if tc_out:
                    msg["tool_calls"] = tc_out

            if role == "tool" and "tool_call_id" in item:
                msg["tool_call_id"] = str(item["tool_call_id"])
            if role == "tool":
                if "_tool_name" in item:
                    msg["_tool_name"] = str(item.get("_tool_name", ""))
                ts = item.get("_ts")
                if isinstance(ts, (int, float)):
                    msg["_ts"] = float(ts)

            out.append(msg)
        return out

    @classmethod
    def _encode_messages(cls, messages: List[Dict[str, Any]]) -> str:
        normalized = cls._decode_messages(
            json.dumps(messages, ensure_ascii=False, default=str)
        )
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
