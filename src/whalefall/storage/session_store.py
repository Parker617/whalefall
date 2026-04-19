# coding: utf-8
"""
SessionStore：会话历史持久化（SQLite）。

设计要点
--------
- **每条消息一行**：`session_messages` 表一行一条消息，`append_message(s)` 立即
  写入（write-ahead），进程在任何时刻被杀都不会丢已提交的消息。
- **孤儿 tool_calls 容错**：加载时调用 `filter_unresolved_tool_uses`，把
  `assistant(tool_calls)` 但缺少对应 `tool_result` 的整条 assistant 丢弃——这和
  Claude Code 对本地 JSONL 会话恢复的策略一致，避免把不完整轮次喂给 LLM 导致
  schema 错误。
- **老库自动迁移**：旧 schema `sessions.messages_json` 的大 JSON Blob 启动时
  会被转成 `session_messages` 一行一条，并 DROP 掉旧列（SQLite 3.35+）。
- **容量治理**：TTL / 最大会话数 / 每会话消息上限 / DB 体积上限。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from whalefall.core.log import get_logger
from whalefall.core.runtime import sessions_db_path

_logger = get_logger("whalefall.session_store")

DEFAULT_MAX_SESSIONS = 200
DEFAULT_MAX_MESSAGES_PER_SESSION = 400
DEFAULT_TTL_DAYS = 30
DEFAULT_MAX_DB_BYTES = 200 * 1024 * 1024  # 200MB

# session_messages 永不保留 system —— system 每次 submit 由 render_system_prompt
# 重新生成（对齐 Claude Code：history 只存 user/assistant/tool）
_VALID_ROLES = {"user", "assistant", "tool"}


def _now_ts() -> int:
    return int(time.time())


# ---------------------------------------------------------------------------
#                     消息编解码 / 孤儿 tool 调用过滤
# ---------------------------------------------------------------------------
def _normalize_message(item: Any) -> Optional[Dict[str, Any]]:
    """把任意入参规整成 {role, content, ...} 标准字典；非法消息返回 None。"""
    if not isinstance(item, dict):
        return None
    role = str(item.get("role", "")).strip().lower()
    if role not in _VALID_ROLES:
        return None
    msg: Dict[str, Any] = {"role": role, "content": str(item.get("content", ""))}

    if role == "assistant" and isinstance(item.get("tool_calls"), list):
        tc_out: List[Dict[str, Any]] = []
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

    if role == "tool":
        if "tool_call_id" in item:
            msg["tool_call_id"] = str(item["tool_call_id"])
        if "_tool_name" in item:
            msg["_tool_name"] = str(item.get("_tool_name", ""))
        ts = item.get("_ts")
        if isinstance(ts, (int, float)):
            msg["_ts"] = float(ts)
    return msg


def filter_unresolved_tool_uses(
    messages: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    孤儿 tool_use 清理（对齐 Claude Code `filterUnresolvedToolUses`）：
      - 收集所有 assistant.tool_calls[*].id 集合与 tool.tool_call_id 集合
      - 若 assistant 的所有 tool_calls 都没匹配到 tool_result → 整条 assistant 丢弃
      - 孤儿 tool（无对应 tool_call_id）也一并丢弃，避免 LLM 抛"无 tool_use 配对"错

    这样在崩溃恢复时能自动跳过没写完的一轮，让下一次 submit 从干净状态起步。
    """
    tool_use_ids: set[str] = set()
    tool_result_ids: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list):
            for tc in m["tool_calls"]:
                tcid = str(tc.get("id") or "") if isinstance(tc, dict) else ""
                if tcid:
                    tool_use_ids.add(tcid)
        elif m.get("role") == "tool":
            tcid = str(m.get("tool_call_id") or "")
            if tcid:
                tool_result_ids.add(tcid)

    unresolved = tool_use_ids - tool_result_ids
    orphan_results = tool_result_ids - tool_use_ids

    if not unresolved and not orphan_results:
        return list(messages)

    out: List[Dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list):
            ids = [str(tc.get("id") or "") for tc in m["tool_calls"] if isinstance(tc, dict)]
            ids = [i for i in ids if i]
            if ids and all(i in unresolved for i in ids):
                # 所有 tool_call 都孤儿 → 整条 assistant 丢弃
                continue
        if m.get("role") == "tool":
            tcid = str(m.get("tool_call_id") or "")
            if tcid and tcid in orphan_results:
                continue
        out.append(m)
    return out


# ---------------------------------------------------------------------------
#                              SessionStore
# ---------------------------------------------------------------------------
class SessionStore:
    """SQLite 会话存储（线程安全，消息即时写盘）。"""

    def __init__(self, db_path: Optional[str | Path] = None):
        self.path = (
            Path(db_path).expanduser().resolve() if db_path else sessions_db_path()
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._session_locks: Dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()
        self._ensure_schema()

    def _session_lock(self, sid: str) -> threading.Lock:
        """某个 session 的内存级锁。同一 sid 的所有写操作串行化。"""
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
        """
        加载某会话的全部消息（按原始插入顺序）。

        加载时会自动丢弃孤儿 tool_calls（见 `filter_unresolved_tool_uses`），
        调用方拿到的是可以直接喂给 LLM 的干净历史。
        """
        sid = (session_id or "").strip()
        if not sid:
            return []
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT role, content, tool_calls_json, tool_call_id, tool_name, ts
                    FROM session_messages
                    WHERE session_id = ?
                    ORDER BY ordinal ASC
                    """,
                    (sid,),
                ).fetchall()
            finally:
                conn.close()
        raw: List[Dict[str, Any]] = []
        for role, content, tc_json, tc_id, tool_name, ts in rows:
            item: Dict[str, Any] = {"role": role, "content": content or ""}
            if role == "assistant" and tc_json:
                try:
                    tc_list = json.loads(tc_json)
                except Exception:
                    tc_list = None
                if isinstance(tc_list, list):
                    item["tool_calls"] = tc_list
            if role == "tool":
                if tc_id:
                    item["tool_call_id"] = tc_id
                if tool_name:
                    item["_tool_name"] = tool_name
                if isinstance(ts, (int, float)):
                    item["_ts"] = float(ts)
            raw.append(item)
        return filter_unresolved_tool_uses(raw)

    def append_message(
        self, session_id: str, message: Dict[str, Any]
    ) -> Optional[int]:
        """写入一条消息并立即落盘；返回新插入行的 ordinal（失败/空返回 None）。"""
        sid = (session_id or "").strip()
        normalized = _normalize_message(message) if sid else None
        if not sid or normalized is None:
            return None
        return self._append_batch(sid, [normalized])[0] if sid else None

    def append_messages(
        self, session_id: str, new_messages: Iterable[Dict[str, Any]]
    ) -> int:
        """
        原子追加一批消息，返回总消息数（含此前已有的）。空列表返回当前总数。

        每条消息一行，作为单次事务一次性提交；避免"写到一半崩"留下不一致。
        """
        sid = (session_id or "").strip()
        if not sid:
            return 0
        normalized = [m for m in (_normalize_message(x) for x in new_messages) if m]
        if not normalized:
            return self.count_messages(sid)
        ordinals = self._append_batch(sid, normalized)
        return ordinals[-1] + 1 if ordinals else self.count_messages(sid)

    def replace_session(
        self, session_id: str, messages: Iterable[Dict[str, Any]]
    ) -> int:
        """
        原子替换某会话的全部消息（压缩回写 / 测试用）。先清空，再批量 append。
        返回写入后的消息条数。
        """
        sid = (session_id or "").strip()
        if not sid:
            return 0
        normalized = [m for m in (_normalize_message(x) for x in messages) if m]
        now = _now_ts()
        with self._session_lock(sid):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM session_messages WHERE session_id = ?", (sid,))
                conn.execute(
                    """
                    INSERT INTO sessions(session_id, updated_at, created_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(session_id)
                    DO UPDATE SET updated_at=excluded.updated_at
                    """,
                    (sid, now, now),
                )
                for idx, msg in enumerate(normalized):
                    self._insert_message_row(conn, sid, idx, msg, now)
                conn.commit()
                return len(normalized)
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                _logger.warning("session replace failed | sid=%s err=%s", sid, exc)
                raise
            finally:
                conn.close()

    # 兼容旧调用路径（会话最终落盘用：先整表替换）。
    # 新代码应优先使用 append_message / append_messages 以获得即时落盘语义。
    save_session = replace_session

    def clear_session(self, session_id: str) -> None:
        self.replace_session(session_id, [])

    def count_messages(self, session_id: str) -> int:
        sid = (session_id or "").strip()
        if not sid:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM session_messages WHERE session_id = ?",
                    (sid,),
                ).fetchone()
            finally:
                conn.close()
        return int(row[0]) if row else 0

    def delete_older_than(self, days: int) -> int:
        """删除 N 天前更新的所有会话，返回删除的会话数。"""
        cutoff = _now_ts() - max(1, int(days)) * 86400
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "DELETE FROM sessions WHERE updated_at < ?", (cutoff,)
                )
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
        """
        按最后活跃时间降序返回最近 N 个会话，附带轮数与 user 首句预览。
        用于 Web 侧栏与 CLI /resume 列表。
        """
        lim = max(1, int(limit))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT s.session_id, s.updated_at,
                           (SELECT COUNT(*) FROM session_messages m
                              WHERE m.session_id = s.session_id AND m.role = 'user') AS turns,
                           (SELECT content FROM session_messages m
                              WHERE m.session_id = s.session_id AND m.role = 'user'
                              ORDER BY ordinal ASC LIMIT 1) AS first_user
                    FROM sessions s
                    ORDER BY s.updated_at DESC
                    LIMIT ?
                    """,
                    (lim,),
                ).fetchall()
            finally:
                conn.close()
        result: List[Dict[str, Any]] = []
        for sid, ts, turns, first_user in rows:
            preview = ((first_user or "")[:50]).strip().replace("\n", " ")
            result.append({
                "session_id": sid,
                "turns": int(turns or 0),
                "updated_at": int(ts or 0),
                "preview": preview,
            })
        return result

    def enforce_limits(
        self,
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_messages_per_session: int = DEFAULT_MAX_MESSAGES_PER_SESSION,
        ttl_days: int = DEFAULT_TTL_DAYS,
        max_db_bytes: int = DEFAULT_MAX_DB_BYTES,
    ) -> Dict[str, int]:
        """TTL 清理 / 每会话裁剪 / 会话总数上限 / DB 体积上限。"""
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
                cur = conn.execute(
                    "DELETE FROM sessions WHERE updated_at < ?", (ttl_cutoff,)
                )
                stats["deleted_ttl"] = int(cur.rowcount or 0)

                # 每会话消息数裁剪：按 ordinal 留最后 N 条，删除其余
                overflow = conn.execute(
                    """
                    SELECT session_id, COUNT(*) FROM session_messages
                    GROUP BY session_id HAVING COUNT(*) > ?
                    """,
                    (max_messages_per_session,),
                ).fetchall()
                for sid, _cnt in overflow:
                    conn.execute(
                        """
                        DELETE FROM session_messages
                         WHERE session_id = ?
                           AND ordinal NOT IN (
                               SELECT ordinal FROM session_messages
                                WHERE session_id = ?
                                ORDER BY ordinal DESC
                                LIMIT ?
                           )
                        """,
                        (sid, sid, max_messages_per_session),
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
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _append_batch(
        self, sid: str, messages: List[Dict[str, Any]]
    ) -> List[int]:
        """内部：对单 session 批量写消息；返回各条写入后的 ordinal。"""
        if not messages:
            return []
        now = _now_ts()
        with self._session_lock(sid):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT COALESCE(MAX(ordinal), -1) FROM session_messages WHERE session_id = ?",
                    (sid,),
                ).fetchone()
                next_ord = int(row[0]) + 1
                conn.execute(
                    """
                    INSERT INTO sessions(session_id, updated_at, created_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(session_id)
                    DO UPDATE SET updated_at=excluded.updated_at
                    """,
                    (sid, now, now),
                )
                ords: List[int] = []
                for offset, msg in enumerate(messages):
                    ordinal = next_ord + offset
                    self._insert_message_row(conn, sid, ordinal, msg, now)
                    ords.append(ordinal)
                conn.commit()
                return ords
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                _logger.warning("session append failed | sid=%s err=%s", sid, exc)
                raise
            finally:
                conn.close()

    @staticmethod
    def _insert_message_row(
        conn: sqlite3.Connection,
        sid: str,
        ordinal: int,
        msg: Dict[str, Any],
        now_ts: int,
    ) -> None:
        tc_json = (
            json.dumps(msg["tool_calls"], ensure_ascii=False, separators=(",", ":"))
            if msg.get("role") == "assistant" and msg.get("tool_calls")
            else None
        )
        tc_id = msg.get("tool_call_id") if msg.get("role") == "tool" else None
        tool_name = msg.get("_tool_name") if msg.get("role") == "tool" else None
        ts = (
            float(msg.get("_ts", now_ts))
            if msg.get("role") == "tool"
            else float(now_ts)
        )
        conn.execute(
            """
            INSERT INTO session_messages(
                session_id, ordinal, role, content,
                tool_calls_json, tool_call_id, tool_name, ts
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                ordinal,
                str(msg.get("role") or ""),
                str(msg.get("content") or ""),
                tc_json,
                tc_id,
                tool_name,
                ts,
            ),
        )

    def _ensure_schema(self) -> None:
        """建表 + 老库迁移（`sessions.messages_json` → `session_messages`）。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id  TEXT PRIMARY KEY,
                        updated_at  INTEGER NOT NULL,
                        created_at  INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_messages (
                        session_id       TEXT NOT NULL,
                        ordinal          INTEGER NOT NULL,
                        role             TEXT NOT NULL,
                        content          TEXT NOT NULL,
                        tool_calls_json  TEXT,
                        tool_call_id     TEXT,
                        tool_name        TEXT,
                        ts               REAL NOT NULL,
                        PRIMARY KEY(session_id, ordinal),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sessions_updated_at "
                    "ON sessions(updated_at)"
                )
                conn.commit()

                self._migrate_legacy_messages_json(conn)
            finally:
                conn.close()

    def _migrate_legacy_messages_json(self, conn: sqlite3.Connection) -> None:
        """
        老库（messages_json blob）→ 新 schema（session_messages 每行一条）。
        只在第一次升级时跑一次，完成后 DROP COLUMN messages_json（SQLite 3.35+）。
        """
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "messages_json" not in existing_cols:
            return  # 已经是新 schema

        legacy_rows = conn.execute(
            "SELECT session_id, messages_json, updated_at FROM sessions"
        ).fetchall()
        if legacy_rows:
            _logger.info(
                "migrating legacy sessions.messages_json → session_messages "
                "(rows=%d)", len(legacy_rows)
            )
        try:
            conn.execute("BEGIN IMMEDIATE")
            for sid, raw, ts in legacy_rows:
                try:
                    arr = json.loads(raw or "[]")
                except Exception:
                    arr = []
                if not isinstance(arr, list):
                    arr = []
                # 清空该 sid 在新表里的内容避免重复
                conn.execute(
                    "DELETE FROM session_messages WHERE session_id = ?", (sid,)
                )
                for idx, item in enumerate(arr):
                    norm = _normalize_message(item)
                    if norm is None:
                        continue
                    self._insert_message_row(conn, sid, idx, norm, int(ts or _now_ts()))
            try:
                conn.execute("ALTER TABLE sessions DROP COLUMN messages_json")
            except sqlite3.OperationalError:
                _logger.info(
                    "DROP COLUMN messages_json not supported on this SQLite; "
                    "column kept but no longer used"
                )
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            _logger.warning("legacy messages_json migration failed | err=%s", exc)
