# coding: utf-8
"""
ToolExecutor：工具并发执行器。

核心机制：
- is_concurrency_safe()：只读工具并发，写工具串行
- execute_batch()：pending group 机制，遇到只读工具加入 pending_group 批量 gather；
  遇到写工具先 await pending_group，再串行执行写工具
- doom_loop_check()：连续 3 次相同 tool_name+args 指纹 → raise RuntimeError 打断死循环
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from whalefall.agent.roles import is_write_tool
from whalefall.core.log import Timer, get_logger, truncate

if TYPE_CHECKING:
    from whalefall.tools.base import ToolContext, ToolResult

# 死循环检测阈值（连续相同指纹次数）
DOOM_LOOP_THRESHOLD = 3

# 最大并发工具数（防止线程池被打满；None = 不限制）
MAX_CONCURRENT_TOOLS: int | None = 16

logger = get_logger("whalefall.executor")


def _make_fingerprint(tool_name: str, args: Dict[str, Any]) -> str:
    """生成工具调用指纹（tool_name + args 的稳定 hash）。"""
    try:
        args_str = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        args_str = str(args)
    return hashlib.md5(f"{tool_name}::{args_str}".encode("utf-8")).hexdigest()


class ToolExecutor:
    """
    工具执行调度器。
    - 只读工具（read_only=True）并发 gather
    - 写工具串行执行，执行前等待 pending 只读任务完成
    - 内建工具直接调 builtin_registry[tool_name].execute()
    - MCP 工具调 mcp_client 路由
    """

    def __init__(
        self,
        builtin_registry=None,
        mcp_client=None,
        max_concurrent: int | None = MAX_CONCURRENT_TOOLS,
    ):
        self._registry = builtin_registry
        self._mcp_client = mcp_client
        self._semaphore = asyncio.Semaphore(max_concurrent) if max_concurrent else None

    def is_concurrency_safe(self, tool_name: str) -> bool:
        """判断工具是否可并发执行（写工具永远串行）。"""
        if is_write_tool(tool_name):
            return False
        if self._registry is not None:
            builtin = self._registry.get_builtin(tool_name)
            if builtin is not None:
                return getattr(builtin, "read_only", True)
        if self._mcp_client is not None and hasattr(self._mcp_client, "is_read_only"):
            try:
                return bool(self._mcp_client.is_read_only(tool_name))
            except Exception:
                return True
        return True

    async def _dispatch(self, tool_call: Dict[str, Any], ctx: "ToolContext") -> "ToolResult":
        """分发单个工具调用，始终捕获异常返回 ToolResult(is_error=True)。"""
        from whalefall.tools.base import ToolResult

        tool_name = tool_call.get("function", {}).get("name", "")
        tool_call_id = tool_call.get("id", "")
        args_str = tool_call.get("function", {}).get("arguments", "{}")
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else (args_str or {})
        except json.JSONDecodeError:
            args = {}

        t = Timer()
        if self._semaphore:
            await self._semaphore.acquire()
        try:
            # 内建工具
            if self._registry is not None and self._registry.is_builtin(tool_name):
                builtin = self._registry.get_builtin(tool_name)
                if builtin is None:
                    raise RuntimeError(f"内建工具 {tool_name} 未注册")
                if asyncio.iscoroutinefunction(builtin.execute):
                    content = await builtin.execute(args, ctx)
                else:
                    loop = asyncio.get_running_loop()
                    content = await loop.run_in_executor(None, lambda: builtin.execute(args, ctx))
                return ToolResult(
                    tool_call_id=tool_call_id,
                    name=tool_name,
                    content=str(content),
                    is_error=False,
                    metadata={"latency_ms": t.ms()},
                )

            # MCP 工具
            if self._mcp_client is not None:
                loop = asyncio.get_running_loop()
                content = await loop.run_in_executor(
                    None, lambda: self._mcp_client.call_tool(tool_name, args)
                )
                return ToolResult(
                    tool_call_id=tool_call_id,
                    name=tool_name,
                    content=str(content),
                    is_error=False,
                    metadata={"latency_ms": t.ms()},
                )

            raise RuntimeError(f"未找到工具：{tool_name}")

        except Exception as exc:
            logger.warning(
                "tool dispatch error | name=%s latency_ms=%s err=%s",
                tool_name, t.ms(), truncate(str(exc), 300),
            )
            return ToolResult(
                tool_call_id=tool_call_id,
                name=tool_name,
                content=f"工具执行错误: {exc}",
                is_error=True,
                metadata={"latency_ms": t.ms()},
            )
        finally:
            if self._semaphore:
                self._semaphore.release()

    async def execute_batch(
        self,
        tool_calls: List[Dict[str, Any]],
        ctx: "ToolContext",
        on_tool_start: Optional[Callable] = None,
        on_tool_end: Optional[Callable] = None,
    ) -> List["ToolResult"]:
        """
        批量执行工具调用（pending group 并发机制）：
        1. 只读工具 → 加入 pending_group（不立即 await）
        2. 写工具 → 先 flush pending_group，再串行执行
        3. 末尾 flush 剩余 pending
        """
        result_map: Dict[int, "ToolResult"] = {}
        pending_tasks: List[Tuple[int, asyncio.Task]] = []

        async def _run(idx: int, tc: Dict[str, Any]) -> Tuple[int, "ToolResult"]:
            tool_name = tc.get("function", {}).get("name", "")
            args_str = tc.get("function", {}).get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else (args_str or {})
            except json.JSONDecodeError:
                args = {}
            t0 = Timer()
            if on_tool_start:
                try:
                    on_tool_start(tool_name, args)
                except Exception:
                    pass
            result = await self._dispatch(tc, ctx)
            if on_tool_end:
                try:
                    on_tool_end(tool_name, result.content, t0.ms() / 1000.0)
                except Exception:
                    pass
            return idx, result

        async def _flush() -> None:
            if not pending_tasks:
                return
            done = await asyncio.gather(*[t for _, t in pending_tasks], return_exceptions=True)
            for (idx, _), res in zip(pending_tasks, done):
                if isinstance(res, Exception):
                    from whalefall.tools.base import ToolResult as TR
                    tc = tool_calls[idx]
                    result_map[idx] = TR(
                        tool_call_id=tc.get("id", ""),
                        name=tc.get("function", {}).get("name", ""),
                        content=f"工具执行异常: {res}",
                        is_error=True,
                        metadata={},
                    )
                else:
                    result_map[res[0]] = res[1]
            pending_tasks.clear()

        for i, tc in enumerate(tool_calls):
            tool_name = tc.get("function", {}).get("name", "")
            if self.is_concurrency_safe(tool_name):
                pending_tasks.append((i, asyncio.create_task(_run(i, tc))))
            else:
                await _flush()
                idx, result = await _run(i, tc)
                result_map[idx] = result

        await _flush()
        return [result_map[i] for i in range(len(tool_calls))]

    def doom_loop_check(self, tool_calls_history: List[List[Dict[str, Any]]]) -> None:
        """
        死循环检测：最近 DOOM_LOOP_THRESHOLD 轮工具调用集合完全相同时抛出 RuntimeError。
        """
        if len(tool_calls_history) < DOOM_LOOP_THRESHOLD:
            return

        def _fp(tcs: List[Dict[str, Any]]) -> str:
            items: List[str] = []
            for tc in tcs:
                fn = tc.get("function") if isinstance(tc, dict) else {}
                name = (fn or {}).get("name", "") if isinstance(fn, dict) else ""
                raw_args = (fn or {}).get("arguments", "{}") if isinstance(fn, dict) else "{}"
                args: Any
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args) if raw_args.strip() else {}
                    except Exception:
                        args = raw_args
                else:
                    args = raw_args or {}
                items.append(_make_fingerprint(name, args))
            return "|".join(sorted(items))

        fps = [_fp(r) for r in tool_calls_history[-DOOM_LOOP_THRESHOLD:]]
        if len(set(fps)) == 1 and fps[0]:
            raise RuntimeError(
                f"检测到 Agent 死循环：连续 {DOOM_LOOP_THRESHOLD} 轮执行完全相同的工具调用。"
                f"指纹: {fps[0][:80]}"
            )
