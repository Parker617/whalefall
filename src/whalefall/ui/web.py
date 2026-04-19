# coding: utf-8
"""
Web UI：FastAPI + WebSocket 实时对话。

启动：
  python -m whalefall.main --web [--port 8000]
  python -m whalefall.ui.web [--port 8000]
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import threading

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from whalefall.core.log import get_logger
from whalefall.ui.slash import (
    COMMON_HELP_LINES,
    SlashContext,
    dispatch_common,
    normalize_slash_input,
    parse_slash,
)

# ── 全局单例（所有 WebSocket 连接共享，避免重复初始化 MCP 子进程） ────────

_llm = None
_registry = None
_mcp_client = None
_perm_manager = None
_query_engine = None
_context_window = 128_000
_startup_error: str | None = None
# session_id -> threading.Event，用于中止正在运行的 AgentLoop
_abort_events: dict[str, threading.Event] = {}
# 保护 _abort_events 的增删：避免 /abort HTTP 端点与 WebSocket 主协程并发读写。
_abort_events_lock = threading.Lock()
_default_model: str = "gpt-4o-mini"
_strict_cold_start: bool = False
# 软重载期间屏蔽新请求，避免访问半初始化状态的全局单例
_services_lock = threading.Lock()
_reloading: bool = False
logger = get_logger("whalefall.ui.web")


WEB_HELP_TEXT = (
    "可用命令：\n"
    "  /help              显示帮助\n"
    + "\n".join(f"  {line}" for line in COMMON_HELP_LINES if not line.startswith("/help"))
    + "\n\n提示：模型和 Agent 类型请使用页面顶部下拉框切换。\n"
    "当前 Web 默认是热启动模式：会话历史持久化，刷新页面后可通过 /resume 恢复。"
)


def _handle_web_slash_command(query: str, session_id: str) -> tuple[bool, str]:
    """
    处理 Web 端斜杠命令。
    Returns:
      (handled, message)
    """
    command, _arg = parse_slash(query)

    if command == "/help":
        return True, WEB_HELP_TEXT

    if not command:
        return False, ""

    if _query_engine is None:
        return True, "Web 服务未完成初始化"

    ctx = SlashContext(
        query_engine=_query_engine,
        session_id=session_id,
        strict_cold_start=_strict_cold_start,
    )
    result = dispatch_common(query, ctx)
    if result.handled:
        return True, result.message
    return True, f"未知命令: {command}。输入 /help 查看帮助。"


def _teardown_services() -> None:
    """释放 LLM / MCP / QueryEngine 等单例，供 reload / shutdown 复用。"""
    global _llm, _registry, _mcp_client, _perm_manager, _query_engine
    if _mcp_client is not None:
        try:
            _mcp_client.disconnect()
        except Exception:
            logger.exception("teardown: mcp disconnect failed")
    _llm = None
    _registry = None
    _mcp_client = None
    _perm_manager = None
    _query_engine = None


def _init_services(*, tag: str = "startup") -> str | None:
    """
    初始化 LLM / Registry / Permission / MCP / QueryEngine 全局单例。

    返回 None 表示成功，返回字符串表示致命错误（LLM 没起来则 _query_engine 为空）。
    MCP 连接失败不视为致命——主循环会自然降级为"仅本地工具"。
    """
    global _llm, _registry, _mcp_client, _perm_manager, _query_engine
    global _startup_error, _context_window, _strict_cold_start

    from whalefall.llm.llm_client import LLMClient
    from whalefall.tools.registry import build_default_registry
    from whalefall.permissions.manager import PermissionManager
    from whalefall.agent.loop import AgentLoop
    from whalefall.agent.query_engine import QueryEngine
    from whalefall.agent.hooks import build_default_hook_manager

    _startup_error = None
    _strict_cold_start = (
        os.getenv("WHALEFALL_WEB_COLD_START", "0").strip().lower()
        not in {"0", "false", "no", "off"}
    )
    web_bypass = (
        os.getenv("WHALEFALL_WEB_BYPASS", "0").strip().lower()
        not in {"0", "false", "no", "off"}
    )

    try:
        _llm = LLMClient(model=_default_model)
        _registry = build_default_registry()
        if web_bypass:
            _perm_manager = PermissionManager.create_bypass()
            logger.warning("[%s] Web 权限：WHALEFALL_WEB_BYPASS=1，跳过所有权限检查", tag)
        else:
            _perm_manager = PermissionManager.create_non_interactive()
            logger.info("[%s] Web 权限：非交互模式；WHALEFALL_WEB_BYPASS=1 可全量放行", tag)

        from whalefall.llm.config import get_model_context
        _context_window = get_model_context(_llm.model)
        logger.info(
            "[%s] LLM 已加载 | 别名=%r | 请求 model=%r | context=%s",
            tag, _llm.model, _llm.model_name, _context_window,
        )
    except Exception as e:
        _startup_error = f"LLM 初始化失败: {e}"
        logger.error("[%s] %s", tag, _startup_error)

    try:
        from whalefall.mcp import MCPClient
        _mcp_client = MCPClient()
        _mcp_client.connect()
        logger.info("[%s] MCP 工具已加载: %d 个", tag, len(_mcp_client.list_tools()))
    except Exception as e:
        _mcp_client = None
        logger.warning("[%s] MCP 连接跳过: %s", tag, e)

    if _llm is not None and _registry is not None and _perm_manager is not None:
        agent_loop = AgentLoop(
            llm_client=_llm,
            tool_registry=_registry,
            mcp_client=_mcp_client,
            permission_manager=_perm_manager,
            context_window_tokens=_context_window,
            hook_manager=build_default_hook_manager(),
        )
        _query_engine = QueryEngine(
            agent_loop,
            enable_persistence=not _strict_cold_start,
        )

    logger.info("[%s] 就绪（strict_cold_start=%s）", tag, _strict_cold_start)
    return _startup_error


@asynccontextmanager
async def lifespan(app: FastAPI):
    with _services_lock:
        _init_services(tag="startup")
    yield
    with _services_lock:
        _teardown_services()


app = FastAPI(title="whalefall", lifespan=lifespan)

_static_dir = Path(__file__).parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/")
async def index():
    return HTMLResponse((_static_dir / "index.html").read_text(encoding="utf-8"))


@app.get("/health")
async def health():
    """前端软/硬重载后轮询此端点判断服务是否就绪。"""
    mcp_tool_count = 0
    if _mcp_client is not None:
        try:
            mcp_tool_count = len(_mcp_client.list_tools())
        except Exception:
            mcp_tool_count = 0
    return {
        "ok": (_startup_error is None) and (_query_engine is not None) and (not _reloading),
        "reloading": _reloading,
        "mcp": _mcp_client is not None,
        "mcp_tool_count": mcp_tool_count,
        "model": getattr(_llm, "model", None) if _llm else None,
        "error": _startup_error,
    }


@app.post("/api/reload")
async def api_reload():
    """
    软重载：重建 LLM / MCPClient / QueryEngine，重读 llm_config.ini 与 mcp/config.yaml。

    注意：
    - 进程不重启，WebSocket 不会断开。
    - Python 源代码的修改不生效（需要用 /api/restart 做硬重启）。
    - 正在执行中的对话会继续跑完（持有的是老引用），新对话走新引擎。
    """
    global _reloading

    def _do_reload() -> str | None:
        global _reloading
        with _services_lock:
            _reloading = True
            try:
                _teardown_services()
                return _init_services(tag="reload")
            finally:
                _reloading = False

    err = await asyncio.to_thread(_do_reload)
    if err:
        return {"ok": False, "error": err}
    mcp_count = len(_mcp_client.list_tools()) if _mcp_client else 0
    return {
        "ok": True,
        "mcp_tool_count": mcp_count,
        "model": getattr(_llm, "model", None) if _llm else None,
        "message": "配置已重载（LLM/MCP/QueryEngine 重建）。Python 代码改动仍需硬重启。",
    }


@app.post("/api/restart")
async def api_restart():
    """
    硬重启：用 os.execv 自替换当前进程，恢复所有 Python 代码改动。

    - WebSocket 会断开约 3~5 秒，前端应轮询 /health 并自动重连。
    - 启动参数完全复用当前 sys.argv，跟原来的启动方式一致。
    """
    def _exec_later():
        # 先让响应发回前端，再 execv 自替换
        time.sleep(0.4)
        try:
            _teardown_services()
        except Exception:
            pass
        logger.warning("[restart] os.execv → %s %s", sys.executable, sys.argv)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_exec_later, daemon=True).start()
    return {
        "ok": True,
        "message": "进程正在重启，WebSocket 将断开约 3~5 秒后自动重连。",
    }


@app.get("/api/sessions")
async def api_list_sessions(limit: int = 40):
    if _query_engine is None:
        return {"sessions": []}
    return {"sessions": _query_engine.list_sessions(limit=limit)}


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: str):
    if _query_engine is None:
        return {"ok": False, "error": "未初始化"}
    _query_engine.drop_session(session_id)
    return {"ok": True, "session_id": session_id}


@app.delete("/api/sessions/older-than/{days}")
async def api_delete_older_than(days: int):
    if _query_engine is None:
        return {"ok": False, "error": "未初始化"}
    if days < 1:
        return {"ok": False, "error": "days 必须 >= 1"}
    count = _query_engine.delete_sessions_older_than(days)
    return {"ok": True, "deleted": count, "days": days}


@app.post("/api/sessions/{session_id}/abort")
async def api_abort_session(session_id: str):
    with _abort_events_lock:
        ev = _abort_events.get(session_id)
    if ev is not None:
        ev.set()
        return {"ok": True, "session_id": session_id}
    return {"ok": False, "reason": "no active run"}


@app.get("/api/sessions/{session_id}/messages")
async def api_session_messages(session_id: str):
    if _query_engine is None:
        return {"messages": [], "session_id": session_id}
    raw = _query_engine.get_session_messages(session_id)
    display = []
    for m in raw:
        role = m.get("role", "")
        content = m.get("content", "") or ""
        if not isinstance(content, str):
            continue
        content = content.strip()
        if role == "user" and content:
            display.append({"role": "user", "text": content})
        elif role == "assistant" and content:
            display.append({"role": "assistant", "text": content})
    return {"messages": display, "session_id": session_id}


# ── WebSocket 主端点 ───────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()

    main_loop = asyncio.get_running_loop()
    # WebSocket 级默认 session_id（可被客户端自定义 session_id 覆盖）
    default_session_id = f"ws-{uuid4().hex[:12]}"

    if _query_engine is None:
        await websocket.send_json({"type": "error", "message": _startup_error or "Web 服务未完成初始化"})
        await websocket.close()
        return

    from whalefall.agent.roles import get_agent

    try:
        while True:
            raw = await websocket.receive_json()
            query = (raw.get("message") or "")
            if not query:
                continue
            query = normalize_slash_input(query)
            if not query:
                continue
            if _reloading or _query_engine is None:
                await websocket.send_json({
                    "type": "system",
                    "message": "服务正在重载配置，请稍候再发送…",
                })
                continue
            session_id = str(raw.get("session_id") or default_session_id)

            # 显式 reset 标志（兼容旧客户端）
            if bool(raw.get("reset", False)):
                _query_engine.clear_session(session_id)
                await websocket.send_json({"type": "system", "message": "会话上下文已清空"})
                continue

            # 斜杠命令（由后端处理，不交给模型）
            handled, msg = _handle_web_slash_command(query, session_id)
            if handled:
                await websocket.send_json({"type": "system", "message": msg})
                continue

            agent_type = raw.get("agent_type", "general")
            model = raw.get("model") or None
            agent_config = get_agent(agent_type)

            # ── 回调：从 AgentLoop 子线程安全发送到 FastAPI 主事件循环 ──
            pending_sends = []
            send_lock = threading.Lock()
            max_pending_sends = max(
                8,
                int(os.getenv("WHALEFALL_WS_MAX_PENDING_SENDS", "128") or "128"),
            )

            def _drain_done_locked() -> None:
                pending_sends[:] = [f for f in pending_sends if not f.done()]

            def _send(msg: dict):
                fut = asyncio.run_coroutine_threadsafe(
                    websocket.send_json(msg), main_loop
                )
                oldest = None
                with send_lock:
                    pending_sends.append(fut)
                    _drain_done_locked()
                    if len(pending_sends) > max_pending_sends:
                        oldest = pending_sends.pop(0)
                if oldest is not None:
                    # 锁外等：避免短暂 5s 背压阻塞其它工具回调线程排队持锁。
                    try:
                        oldest.result(timeout=5)
                    except Exception:
                        pass

            def on_text(delta: str):
                _send({"type": "text", "delta": delta})

            def on_tool_start(name: str, args: dict):
                # args 可能含不可序列化的值，做简单保护
                try:
                    import json as _json
                    _json.dumps(args)
                except Exception:
                    args = {k: str(v) for k, v in args.items()}
                _send({"type": "tool_start", "name": name, "args": args})

            def on_tool_end(name: str, result: str, elapsed: float):
                is_err = (result or "").startswith(("错误", "Error", "error"))
                _send({
                    "type": "tool_end",
                    "name": name,
                    "elapsed": round(elapsed, 2),
                    "is_error": is_err,
                })

            def on_compaction(before: int, after: int):
                _send({"type": "compaction", "before": before, "after": after})

            # ── 在线程池中运行 AgentLoop（不阻塞事件循环） ──
            abort_ev = threading.Event()
            with _abort_events_lock:
                _abort_events[session_id] = abort_ev
            try:
                result = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: _query_engine.submit(
                        session_id=session_id,
                        user_query=query,
                        agent_config=agent_config,
                        model=model,
                        request_id=f"{session_id}-{uuid4().hex[:8]}",
                        on_text=on_text,
                        on_tool_start=on_tool_start,
                        on_tool_end=on_tool_end,
                        on_compaction=on_compaction,
                        abort_event=abort_ev,
                    ),
                )
                aborted = abort_ev.is_set()
                await websocket.send_json({
                    "type": "done",
                    "result": result,
                    "session_id": session_id,
                    "aborted": aborted,
                })
            except Exception as e:
                logger.exception("websocket query failed | session_id=%s", session_id)
                try:
                    await websocket.send_json({"type": "error", "message": str(e)})
                except Exception:
                    pass
            finally:
                with _abort_events_lock:
                    if _abort_events.get(session_id) is abort_ev:
                        _abort_events.pop(session_id, None)
                with send_lock:
                    futures = list(pending_sends)
                    pending_sends.clear()
                for fut in futures:
                    try:
                        fut.result(timeout=2)
                    except Exception:
                        pass

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("websocket endpoint crashed")
        try:
            await websocket.close()
        except Exception:
            pass


# ── 独立启动入口 ──────────────────────────────────────────────────────────

def run(host: str = "0.0.0.0", port: int = 8000, reload: bool = False, model: str = "gpt-4o-mini"):
    global _default_model
    _default_model = model
    import uvicorn
    uvicorn.run(
        "whalefall.ui.web:app",
        host=host,
        port=port,
        reload=reload,
    )


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--reload", action="store_true")
    args = p.parse_args()
    run(host=args.host, port=args.port, reload=args.reload)
