# coding: utf-8
"""
MCPClient — MCP 连接管理器。

- 支持 stdio / SSE / HTTP 三种 transport
- 工具发现：读取 MCP annotations 正确映射 is_read_only / is_destructive
- 断线重连：call_tool 失败时自动重连目标 server 并重试一次
- Session pool：每个 server 维护后台事件循环上的单一 session
"""
from __future__ import annotations

import asyncio
import os
import threading
import yaml
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.sse import sse_client

from whalefall.core.log import get_logger

logger = get_logger("whalefall.mcp.client")

CONNECT_TIMEOUT = 30    # 秒
RECONNECT_TIMEOUT = 15
CALL_TIMEOUT = 60       # 单次工具调用上限（秒）；防止 stdio server 卡死


# 用户既没指定 config_path，也没设 WHALEFALL_MCP_CONFIG，默认 config.yaml 又不存在时
# 使用的内建回退配置：拉起随包的演示 MCP server（hello.py 插件）。
# 这样 `pip install whalefall` 后无需复制任何模板文件即可跑通链路。
_DEFAULT_DEMO_CONFIG: Dict[str, Any] = {
    "servers": {
        "demo": {
            "type": "stdio",
            "command": "python",
            "args": ["-m", "whalefall.mcp.server"],
            "description": "Built-in demo MCP server (echo / add / time_now).",
        }
    }
}


def _load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    解析 MCP 配置。加载顺序：
      1. 显式传入的 config_path（必须存在，否则抛错）
      2. 环境变量 WHALEFALL_MCP_CONFIG（必须存在，否则抛错）
      3. 包内默认路径 `mcp/config.yaml`（存在则读，不存在则回退到内建 demo）
    """
    explicit = config_path or os.getenv("WHALEFALL_MCP_CONFIG", "")
    if explicit:
        with open(explicit, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    default_path = Path(__file__).parent / "config.yaml"
    if default_path.exists():
        with open(default_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    logger.info(
        "MCP 默认配置 %s 不存在，回退到内建 demo server（hello 插件）。"
        "要接入自定义 MCP，请复制 config.yaml.example 为 config.yaml 并编辑。",
        default_path,
    )
    return _DEFAULT_DEMO_CONFIG


class MCPClient:
    """
    MCP 客户端，管理所有 server 连接，提供工具发现和调用。

    用法：
        client = MCPClient()
        client.connect()
        tools = client.list_tools()
        result = client.call_tool("demo__echo", {"text": "hello"})
        client.disconnect()
    """

    def __init__(self, config_path: Optional[str] = None):
        self._sessions: Dict[str, ClientSession] = {}
        self._tools: List[Dict[str, Any]] = []
        self._tool_map: Dict[str, Tuple[str, str]] = {}    # api_name → (server, mcp_name)
        self._tool_meta: Dict[str, Dict[str, Any]] = {}
        self._exit_stack = AsyncExitStack()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_requested: threading.Event | None = None
        self._config = _load_config(config_path)

    # ── 连接管理 ─────────────────────────────────────────────────────────

    def connect(self) -> None:
        """同步连接所有配置的 server，启动后台 event loop。"""
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            if self._sessions:
                return
            raise RuntimeError("MCPClient 后台线程仍在运行但未就绪，请先 disconnect 或重建客户端")

        self._sessions.clear()
        self._tools.clear()
        self._tool_map.clear()
        self._tool_meta.clear()
        self._exit_stack = AsyncExitStack()
        self._loop = asyncio.new_event_loop()
        self._stop_requested = threading.Event()

        ready = threading.Event()
        startup_error: Dict[str, Exception] = {}
        loop = self._loop
        stop_requested = self._stop_requested

        def _run():
            assert loop is not None
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._connect_all())
            except Exception as exc:
                startup_error["error"] = exc
            finally:
                ready.set()
            if "error" not in startup_error and not stop_requested.is_set():
                loop.run_forever()
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        if not ready.wait(timeout=CONNECT_TIMEOUT):
            stop_requested.set()
            if self._stop_background_loop(wait=5):
                self._clear_runtime_refs()
            raise TimeoutError(f"MCPClient 连接超时（{CONNECT_TIMEOUT}s）")
        if "error" in startup_error:
            stop_requested.set()
            if self._stop_background_loop(wait=5):
                self._clear_runtime_refs()
            raise RuntimeError(f"MCPClient 连接失败: {startup_error['error']}")
        logger.info("MCPClient connected: %s", list(self._sessions.keys()))

    def disconnect(self) -> None:
        """断开所有 MCP 连接，停止后台事件循环。"""
        if self._loop is None:
            return
        if self._stop_requested is not None:
            self._stop_requested.set()
        try:
            self._run_on_loop(self._exit_stack.aclose(), timeout=10, op="mcp_exit_stack_aclose")
        except Exception:
            pass
        stopped = self._stop_background_loop(wait=10)
        if not stopped:
            logger.error("MCPClient disconnect timeout: background thread still alive, keep handles")
            return
        self._clear_runtime_refs()

    def _clear_runtime_refs(self) -> None:
        """清空运行态资源引用（仅在后台线程已停止时调用）。"""
        self._sessions.clear()
        self._tools.clear()
        self._tool_map.clear()
        self._tool_meta.clear()
        self._thread = None
        self._loop = None
        self._stop_requested = None
        self._exit_stack = AsyncExitStack()

    # ── 工具操作 ─────────────────────────────────────────────────────────

    def list_tools(self, servers: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        返回工具列表。

        Args:
            servers: 可选，限定只返回指定 server 名的工具。
                     None 或空列表 = 返回全部。
        """
        if not servers:
            return list(self._tools)
        server_set = set(servers)
        return [t for t in self._tools if t.get("_server") in server_set]

    def server_names(self) -> List[str]:
        """返回所有已连接的 server 名称列表。"""
        return list(self._sessions.keys())

    def is_read_only(self, api_name: str) -> bool:
        return bool((self._tool_meta.get(api_name) or {}).get("read_only", True))

    def is_destructive(self, api_name: str) -> bool:
        return bool((self._tool_meta.get(api_name) or {}).get("destructive", False))

    def get_tool_max_result_chars(self, api_name: str, default: int = 12_000) -> int:
        raw = (self._tool_meta.get(api_name) or {}).get("max_result_chars")
        try:
            return max(0, int(raw)) if raw is not None else default
        except Exception:
            return default

    def call_tool(self, api_name: str, arguments: Dict[str, Any]) -> str:
        """同步调用工具（线程安全），失败时自动重连重试一次。"""
        assert self._loop, "MCPClient not connected"
        try:
            return self._run_on_loop(
                self._call_async(api_name, arguments),
                timeout=CALL_TIMEOUT,
                op=f"call_tool:{api_name}",
            )
        except Exception as exc:
            server_name = self._tool_map.get(api_name, (None,))[0]
            if server_name is None:
                raise
            logger.warning("MCP call failed, reconnecting '%s': %s", server_name, exc)
            try:
                self._run_on_loop(
                    self._reconnect_server(server_name),
                    timeout=RECONNECT_TIMEOUT,
                    op=f"reconnect:{server_name}",
                )
                return self._run_on_loop(
                    self._call_async(api_name, arguments),
                    timeout=CALL_TIMEOUT,
                    op=f"call_tool_retry:{api_name}",
                )
            except Exception as retry_exc:
                logger.error("Reconnect/retry failed for '%s': %s", server_name, retry_exc)
                raise

    def _run_on_loop(self, coro, *, timeout: int, op: str):
        """在线程安全地向后台 loop 提交协程，带超时和取消。"""
        assert self._loop is not None, "MCPClient loop not initialized"
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except FutureTimeoutError as exc:
            fut.cancel()
            raise TimeoutError(f"{op} 超时（{timeout}s）") from exc
        except Exception:
            if not fut.done():
                fut.cancel()
            raise

    def _stop_background_loop(self, *, wait: int) -> bool:
        """停止后台 loop + thread；返回是否成功停止。"""
        loop = self._loop
        thread = self._thread
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=wait)
        return (thread is None) or (not thread.is_alive())

    # ── 异步内部方法 ─────────────────────────────────────────────────────

    async def _connect_all(self) -> None:
        for name, cfg in self._config.get("servers", {}).items():
            try:
                await self._connect_server(name, cfg)
            except Exception as exc:
                logger.warning("Failed to connect server '%s': %s", name, exc)

    async def _connect_server(self, name: str, cfg: Dict) -> None:
        transport_type = cfg.get("type", "stdio")

        if transport_type == "stdio":
            params = StdioServerParameters(
                command=cfg["command"],
                args=cfg.get("args", []),
                env=cfg.get("env"),
            )
            read, write = await self._exit_stack.enter_async_context(stdio_client(params))
        elif transport_type == "sse":
            read, write = await self._exit_stack.enter_async_context(
                sse_client(cfg["url"], headers=cfg.get("headers", {}))
            )
        elif transport_type == "http":
            from mcp.client.streamable_http import streamablehttp_client
            read, write, _ = await self._exit_stack.enter_async_context(
                streamablehttp_client(cfg["url"], headers=cfg.get("headers", {}))
            )
        else:
            raise ValueError(f"Unknown transport type: {transport_type}")

        session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions[name] = session

        response = await session.list_tools()
        for tool in response.tools:
            api_name = f"{name}__{tool.name}"
            annotations = getattr(tool, "annotations", None) or {}
            if hasattr(annotations, "model_dump"):
                annotations = annotations.model_dump()
            elif hasattr(annotations, "__dict__"):
                annotations = vars(annotations)

            destructive_hint = annotations.get("destructiveHint", annotations.get("destructive"))
            is_destructive = bool(destructive_hint) if destructive_hint is not None else False
            read_only_hint = annotations.get("readOnlyHint")
            is_read_only = (
                bool(read_only_hint) if read_only_hint is not None else (not is_destructive)
            )
            raw_max_chars = (
                annotations.get("maxResultSizeChars") or annotations.get("max_result_chars")
                if isinstance(annotations, dict) else None
            )
            try:
                max_result_chars = max(0, int(raw_max_chars)) if raw_max_chars is not None else 12_000
            except Exception:
                max_result_chars = 12_000

            self._tool_map[api_name] = (name, tool.name)
            self._tool_meta[api_name] = {
                "read_only": is_read_only,
                "destructive": is_destructive,
                "max_result_chars": max_result_chars,
            }
            self._tools.append({
                "type": "function",
                "function": {
                    "name": api_name,
                    "description": (tool.description or "")[:2048],
                    "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                },
                "_server": name,
                "_read_only": is_read_only,
                "_destructive": is_destructive,
                "_max_result_chars": max_result_chars,
            })

        logger.info("Server '%s' (%s): %d tools", name, transport_type, len(response.tools))

    async def _reconnect_server(self, server_name: str) -> None:
        cfg = self._config.get("servers", {}).get(server_name)
        if not cfg:
            raise RuntimeError(f"server '{server_name}' not in config")
        self._sessions.pop(server_name, None)
        self._tools = [t for t in self._tools if t.get("_server") != server_name]
        for k in [k for k, v in self._tool_map.items() if v[0] == server_name]:
            del self._tool_map[k]
            self._tool_meta.pop(k, None)
        await self._connect_server(server_name, cfg)
        logger.info("Reconnected to server '%s'", server_name)

    async def _call_async(self, api_name: str, arguments: Dict[str, Any]) -> str:
        server_name, tool_name = self._tool_map[api_name]
        result = await self._sessions[server_name].call_tool(tool_name, arguments)
        return "\n".join(
            c.text if hasattr(c, "text") else str(c)
            for c in (result.content or [])
        )
