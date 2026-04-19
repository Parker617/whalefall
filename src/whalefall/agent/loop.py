"""
AgentLoop：主循环（多轮 LLM + 工具调用）。

核心流程：
while step < max_turns:
  1. await context_manager.check_and_compact(messages)  ← async，不阻塞事件循环
  2. llm.stream_with_tools(messages, tools) → 实时 delta + 结束时 tool_calls
  3. 无 tool_calls → break（纯文本回复，结束）
  4. executor.doom_loop_check(history)
  5. permission_manager.check_batch(tool_calls)  ← 含 BashGuard + 路径约束
  6. results = executor.execute_batch(tool_calls, ctx)
  7. messages.append(assistant_msg + tool_results)
  8. step++

on_text：每次 LLM 返回内容时调用（真实流式 delta）
on_tool_start/on_tool_end：工具执行事件回调
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple, Union

from whalefall.agent.compaction import ContextManager
from whalefall.agent.events import (
    CompactionEvent, DoneEvent, TextDeltaEvent, ToolEndEvent, ToolStartEvent,
)
from whalefall.agent.executor import ToolExecutor
from whalefall.agent.hooks import (
    HookManager,
    HOOK_SESSION_START, HOOK_TOOL_USE_FAILURE,
)
from whalefall.agent.roles import (
    AgentConfig, get_agent, is_write_tool, render_system_prompt,
)
from whalefall.core.log import Timer, get_logger, get_request_logger, new_request_id, truncate
from whalefall.storage.trace import TraceWriter

logger = get_logger("whalefall.loop")

DEFAULT_MAX_TOOL_RESULT_CHARS = 12_000
MAX_RECENTLY_RESTORED_FILES = 5
MAX_RECENTLY_RESTORED_CHARS = 50_000
SKILL_LIST_CHAR_BUDGET = 8_000
MAX_INVOKED_SKILLS_RESTORED = 4
MAX_INVOKED_SKILLS_CHARS = 60_000


def _default_agent() -> AgentConfig:
    """延迟加载默认 general agent，避免模块 import 期触碰文件系统。"""
    return get_agent("general")


class AgentLoop:
    """
    主循环：多轮 LLM + 工具调用。

    支持：
    - 流式回调（on_text）
    - 工具事件回调（on_tool_start/on_tool_end）
    - 三层 Context 压缩
    - 死循环检测
    - 权限检查
    - 子 Agent 递归（通过 AgentTool 触发新 AgentLoop 实例）
    """

    def _build_system_prompt(
        self,
        agent_config: AgentConfig,
        *,
        custom_base: Optional[str] = None,
    ) -> str:
        """
        按 agent_config.include 声明的顺序装配 system prompt。

        所有静态积木（BASE_IDENTITY / GUARDRAILS / TOOL_USAGE_RULES）与动态积木
        （ENV_INFO / AGENT_MD / SYSTEM_PROMPT / TOOL_REFERENCES）都由
        `whalefall.agent.roles.render_system_prompt()` 统一组装。
        """
        return render_system_prompt(
            agent_config,
            registry=self._registry,
            custom_base=custom_base,
        )

    def _build_skill_listing_reminder(self, agent_config: "AgentConfig") -> str:
        """
        构建 skills 目录摘要提醒：
        - 不放进 system prompt 主体
        - 作为单独 reminder 消息注入对话
        - 按 agent_config.allowed_skill_paths 过滤，确保 LLM 看到的目录与它实际可加载的一致
        """
        if self._registry is None or not self._registry.is_builtin("skill"):
            return ""
        try:
            from whalefall.tools.skill import SkillTool
            lines = SkillTool.catalog_lines(
                char_budget=SKILL_LIST_CHAR_BUDGET,
                allowed_paths=agent_config.allowed_skill_paths,
            )
        except Exception as exc:
            self._logger.warning("build skill listing reminder failed | err=%s", str(exc))
            return ""
        if not lines:
            return ""
        return (
            "[系统提醒｜可用 Skills]\n"
            "以下是可用 skill 摘要（name + description）。"
            "当任务匹配某个 skill 时，先调用 `skill` 工具加载其全文再执行：\n"
            + "\n".join(lines)
        )

    def __init__(
        self,
        llm_client,
        tool_registry=None,
        mcp_client=None,
        permission_manager=None,
        hook_manager: Optional[HookManager] = None,
        context_manager: Optional[ContextManager] = None,
        context_window_tokens: int = 128_000,
    ):
        """
        Args:
            llm_client: LLMClient 实例（必须）
            tool_registry: ToolRegistry 实例（可选，有内建工具时需要）
            mcp_client: MCPClient 实例（可选，有 MCP 工具时需要）
            permission_manager: PermissionManager 实例（可选，无则跳过权限检查）
            hook_manager: HookManager 实例（可选，无则使用空 HookManager）
            context_manager: ContextManager 实例（可选，无则按 context_window_tokens 自动创建）
            context_window_tokens: 模型 context window 大小，用于自动推导压缩阈值。
                从 llm_config.ini 的 {model}_max_context 读取后传入。
        """
        self.llm = llm_client
        self._registry = tool_registry
        self._mcp_client = mcp_client
        self._perm_manager = permission_manager
        self._context_manager = context_manager or ContextManager(
            context_window_tokens=context_window_tokens
        )
        self._executor = ToolExecutor(
            builtin_registry=tool_registry,
            mcp_client=mcp_client,
        )
        self._hooks = hook_manager or HookManager()
        self._logger = logger

    @property
    def hooks(self) -> HookManager:
        return self._hooks

    def cleanup(self) -> None:
        """
        显式释放子 Agent 资源。AgentTool 在 finally 中调用。
        防止长会话中 spawn 大量子 Agent 造成内存泄漏。
        """
        # 清理 context manager 的内部缓存
        if hasattr(self._context_manager, "clear"):
            try:
                self._context_manager.clear()
            except Exception:
                pass
        # 断开对共享资源的引用（不 close，因为是从父 Agent 借来的）
        self._registry = None
        self._mcp_client = None
        self._perm_manager = None

    # ------------------------------------------------------------------ #
    #                  主循环：事件流（AsyncGenerator）                     #
    # ------------------------------------------------------------------ #
    async def run_stream(
        self,
        user_query: str,
        *,
        agent_config: Optional[AgentConfig] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        parent_context: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        extra_messages: Optional[List[Dict[str, Any]]] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> AsyncGenerator[
        Union[TextDeltaEvent, ToolStartEvent, ToolEndEvent, CompactionEvent, DoneEvent],
        None,
    ]:
        """
        异步事件流主循环。yield AgentEvent 事件：
          TextDeltaEvent  — LLM 输出文本片段
          ToolStartEvent  — 工具开始执行
          ToolEndEvent    — 工具执行完成
          CompactionEvent — Context 已压缩
          DoneEvent       — Agent 完成（含最终文本）

        用法：
            async for event in agent.run_stream(query):
                if isinstance(event, TextDeltaEvent):
                    print(event.text, end="", flush=True)
                elif isinstance(event, DoneEvent):
                    final = event.text
        """
        if agent_config is None:
            agent_config = _default_agent()

        rid = request_id or new_request_id()
        rlog = get_request_logger(self._logger, request_id=rid, agent_name=agent_config.name)
        tw = TraceWriter(
            rid=rid, sid="-", uid="-",
            agent=agent_config.name,
            model=model or getattr(self.llm, "model", "-"),
            enabled=None,
        )

        effective_model = model or agent_config.model or None
        full_system = self._build_system_prompt(agent_config, custom_base=system_prompt)

        messages: List[Dict[str, Any]] = [{"role": "system", "content": full_system}]
        skill_listing_reminder = self._build_skill_listing_reminder(agent_config)
        if skill_listing_reminder:
            messages.append({"role": "system", "content": skill_listing_reminder})
        if extra_messages:
            messages.extend(extra_messages)
        if parent_context:
            try:
                parent_ctx_text = json.dumps(parent_context, ensure_ascii=False, default=str)
            except Exception:
                parent_ctx_text = str(parent_context)
            messages.append({
                "role": "system",
                "content": "[父 Agent 上下文]\n" + truncate(parent_ctx_text, 6000),
            })
        messages.append({"role": "user", "content": user_query})
        # 本轮会话增量（不含 system），供 QueryEngine 做跨轮持久化
        session_messages: List[Dict[str, Any]] = [
            {"role": "user", "content": user_query}
        ]
        final_assistant_recorded = False

        tw.log_system(full_system)
        tw.log_user(user_query)

        # session_start hook：会话启动时触发，允许外部注入上下文
        session_hook = self._hooks.emit(
            HOOK_SESSION_START,
            {
                "agent_config": agent_config,
                "model": effective_model,
                "query": user_query[:500],
                "request_id": rid,
            },
            logger=rlog,
        )
        extra_context = session_hook.get("additional_context")
        if isinstance(extra_context, str) and extra_context.strip():
            messages.append({"role": "system", "content": extra_context.strip()})

        tools = self._get_tools(agent_config)
        rlog.info(
            'loop start | model=%s max_turns=%s tools=%s query="%s"',
            effective_model or "default", agent_config.max_turns,
            len(tools), truncate(user_query, 200),
        )
        tw.set_tools(tools)

        from whalefall.tools.base import ToolContext
        ctx = ToolContext(
            agent_name=agent_config.name,
            allowed_skill_paths=agent_config.allowed_skill_paths,
            tool_registry=self._registry,
            mcp_client=self._mcp_client,
            llm_client=self.llm,
            permission_manager=self._perm_manager,
            hook_manager=self._hooks,
        )

        # 新会话启动时：检查持久化的未完成任务，提醒 LLM
        pending_tasks_msg = self._build_pending_tasks_reminder(ctx)
        if pending_tasks_msg:
            messages.append({"role": "system", "content": pending_tasks_msg})
            rlog.info("injected pending tasks reminder from persistent store")

        tool_calls_history: List[List[Dict[str, Any]]] = []
        last_content = ""
        step = 0
        stage = "init"

        try:
            while step < agent_config.max_turns:
                if abort_event and abort_event.is_set():
                    rlog.info("loop aborted by user | step=%s", step)
                    break
                step += 1
                rlog.info("step %s/%s start", step, agent_config.max_turns)

                # 1. Context 压缩
                before_len = len(messages)
                compaction_info: Dict[str, int] = {}

                def _on_compaction(before: int, after: int) -> None:
                    compaction_info["before"] = before
                    compaction_info["after"] = after

                messages = await self._context_manager.check_and_compact(
                    messages, llm_client=self.llm,
                    model=effective_model, on_compaction=_on_compaction,
                )
                # 不依赖 ContextManager 实例级标志（并发 session 下会互相覆盖）；
                # 仅使用本次调用的局部回调结果判断是否发生压缩。
                if ("before" in compaction_info) and ("after" in compaction_info):
                    yield CompactionEvent(
                        before_tokens=compaction_info.get("before", before_len),
                        after_tokens=compaction_info.get("after", len(messages)),
                    )

                    restored_msg = self._build_recent_reads_restore_message(ctx)
                    if restored_msg:
                        self._insert_after_last_system(messages, {"role": "system", "content": restored_msg})
                        rlog.info("restored recently_read files after compaction")

                    invoked_skills_msg = self._build_invoked_skills_restore_message(ctx)
                    if invoked_skills_msg:
                        self._insert_after_last_system(messages, {"role": "system", "content": invoked_skills_msg})
                        rlog.info("restored invoked skills after compaction")

                    todo_msg = self._build_todo_restore_message(ctx)
                    if todo_msg:
                        self._insert_after_last_system(messages, {"role": "system", "content": todo_msg})
                        rlog.info("restored todo list after compaction")

                # 2. LLM 调用（异步真实流式）
                stage = "before_llm"
                llm_payload = self._hooks.emit(
                    "before_llm",
                    {
                        "messages": messages,
                        "tools": tools,
                        "model": effective_model,
                        "request_id": rid,
                        "agent_config": agent_config,
                        "step": step,
                    },
                    logger=rlog,
                )
                if isinstance(llm_payload.get("messages"), list):
                    messages = llm_payload["messages"]
                if isinstance(llm_payload.get("tools"), list):
                    tools = llm_payload["tools"]
                if "model" in llm_payload:
                    effective_model = llm_payload.get("model") or effective_model

                t_llm = Timer()
                content_parts: List[str] = []
                tool_calls: List[Dict[str, Any]] = []
                try:
                    stage = "llm_stream"
                    async for item in self.llm.stream_with_tools(
                        messages=messages,
                        tools=tools,
                        model=effective_model,
                    ):
                        if abort_event and abort_event.is_set():
                            rlog.info("stream aborted by user | step=%s", step)
                            break
                        if isinstance(item, str):
                            if item:
                                content_parts.append(item)
                                yield TextDeltaEvent(text=item)
                        elif isinstance(item, list):
                            tool_calls = item
                except Exception as exc:
                    self._hooks.emit(
                        "on_error",
                        {
                            "stage": stage,
                            "error": exc,
                            "step": step,
                            "request_id": rid,
                            "agent_config": agent_config,
                        },
                        logger=rlog,
                    )
                    rlog.error("LLM call failed | step=%s err=%s", step, truncate(str(exc), 300))
                    raise

                content = "".join(content_parts).strip()
                stage = "after_llm"
                llm_after = self._hooks.emit(
                    "after_llm",
                    {
                        "content": content,
                        "tool_calls": tool_calls,
                        "latency_ms": t_llm.ms(),
                        "step": step,
                        "request_id": rid,
                        "agent_config": agent_config,
                    },
                    logger=rlog,
                )
                if isinstance(llm_after.get("content"), str):
                    content = llm_after["content"]
                if isinstance(llm_after.get("tool_calls"), list):
                    tool_calls = llm_after["tool_calls"]

                tw.add_llm_round(i=step, content=content or "", tool_calls=tool_calls)
                rlog.info(
                    "llm reply | step=%s tool_calls=%s latency_ms=%s content_len=%s",
                    step, len(tool_calls or []), t_llm.ms(), len(content or ""),
                )

                last_content = (content or "").strip()

                # 3. 无 tool_calls → 结束
                if not tool_calls:
                    final_msg = {"role": "assistant", "content": (last_content or "（无回复）")}
                    messages.append(final_msg)
                    session_messages.append(final_msg)
                    final_assistant_recorded = True
                    rlog.info("loop done | step=%s no_tool_calls", step)
                    break

                # 3.5 abort 检查（流结束后，执行工具前）
                if abort_event and abort_event.is_set():
                    rlog.info("loop aborted before tool exec | step=%s", step)
                    break

                # 4. 死循环检测
                tool_calls_history.append(tool_calls)
                try:
                    self._executor.doom_loop_check(tool_calls_history)
                except RuntimeError as exc:
                    rlog.error("doom loop detected | step=%s err=%s", step, str(exc))
                    last_content = f"Agent 已中止：{exc}"
                    break

                # 5. 权限检查
                denied_idx_set: set[int] = set()
                denied_msgs: Dict[int, str] = {}
                to_execute: List[tuple[int, Dict[str, Any]]] = []
                if self._perm_manager is not None:
                    denied_idx_set, deny_reasons = self._check_permissions(tool_calls, agent_config)
                    denied_msgs.update(deny_reasons)

                for idx, tc in enumerate(tool_calls):
                    if idx in denied_idx_set:
                        pass  # denied_msgs already populated above
                    else:
                        to_execute.append((idx, tc))

                # 5.1 before_tool hooks（允许改写 args）
                stage = "before_tool"
                hooked_to_execute: List[tuple[int, Dict[str, Any]]] = []
                for idx, tc in to_execute:
                    fn_name = tc.get("function", {}).get("name", "")
                    args_str = tc.get("function", {}).get("arguments", "{}")
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else (args_str or {})
                    except Exception:
                        args = {}
                    hook_payload = self._hooks.emit(
                        "before_tool",
                        {
                            "tool_call": tc,
                            "name": fn_name,
                            "args": args,
                            "step": step,
                            "request_id": rid,
                            "agent_config": agent_config,
                        },
                        logger=rlog,
                    )
                    new_args = hook_payload.get("args", args)
                    if not isinstance(new_args, dict):
                        new_args = args
                    if new_args is not args:
                        tc = self._with_tool_call_args(tc, new_args)
                    hooked_to_execute.append((idx, tc))
                to_execute = hooked_to_execute

                # 6. yield ToolStartEvent + 执行
                for _, tc in to_execute:
                    fn_name = tc.get("function", {}).get("name", "")
                    args_str = tc.get("function", {}).get("arguments", "{}")
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else {}
                    except Exception:
                        args = {}
                    yield ToolStartEvent(name=fn_name, args=args, step=step)

                t_tools = Timer()
                results: List[Any] = []
                if to_execute:
                    # 规划模式：不执行工具，生成计划描述
                    if ctx.plan_mode:
                        from whalefall.tools.base import ToolResult
                        results = []
                        for tc in [tc for _, tc in to_execute]:
                            fn = tc.get("function", {})
                            results.append(ToolResult(
                                tool_call_id=tc.get("id", ""),
                                name=fn.get("name", ""),
                                content=f"[规划模式] 计划执行: {fn.get('name', '')}({fn.get('arguments', '{}')})",
                                is_error=False,
                                metadata={},
                            ))
                    else:
                        results = await self._executor.execute_batch(
                            [tc for _, tc in to_execute], ctx,
                        )
                rlog.info(
                    "tools done | step=%s executed=%s denied=%s latency_ms=%s",
                    step, len(results), len(denied_idx_set), t_tools.ms(),
                )

                # 7. yield ToolEndEvent + trace
                result_by_idx: Dict[int, Any] = {}
                for (orig_idx, tc), res in zip(to_execute, results):
                    stage = "after_tool"
                    tool_after = self._hooks.emit(
                        "after_tool",
                        {
                            "name": res.name,
                            "content": res.content,
                            "is_error": res.is_error,
                            "metadata": res.metadata,
                            "step": step,
                            "request_id": rid,
                            "agent_config": agent_config,
                        },
                        logger=rlog,
                    )
                    if isinstance(tool_after.get("content"), str):
                        res.content = tool_after["content"]
                    if "is_error" in tool_after:
                        res.is_error = bool(tool_after.get("is_error"))

                    # tool_use_failure hook：工具执行失败时触发（监控用）
                    if res.is_error:
                        self._hooks.emit(
                            HOOK_TOOL_USE_FAILURE,
                            {
                                "name": res.name,
                                "content": res.content[:1000],
                                "step": step,
                                "request_id": rid,
                                "agent_config": agent_config,
                            },
                            logger=rlog,
                        )

                    result_by_idx[orig_idx] = res
                    elapsed = res.metadata.get("latency_ms", 0) / 1000.0
                    yield ToolEndEvent(
                        name=res.name,
                        content=res.content or "",
                        elapsed=elapsed,
                        is_error=res.is_error,
                        step=step,
                    )
                    args_str = tc.get("function", {}).get("arguments", "{}")
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else {}
                    except Exception:
                        args = {}
                    tw.add_tool_run(
                        tool_call_id=tc.get("id", ""),
                        name=res.name,
                        ok=not res.is_error,
                        latency_ms=res.metadata.get("latency_ms", 0),
                        args=args,
                        result_text=res.content,
                    )

                # 8. 构建工具结果消息（回填到 messages）
                assistant_tool_msg = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                }
                messages.append(assistant_tool_msg)
                session_messages.append(assistant_tool_msg)
                for idx, tc in enumerate(tool_calls):
                    fid = tc.get("id", "")
                    fn_name = tc.get("function", {}).get("name", "")
                    if idx in denied_idx_set:
                        denied_msg = {
                            "role": "tool",
                            "tool_call_id": fid,
                            "_tool_name": fn_name,
                            "_ts": time.time(),
                            "content": denied_msgs.get(idx, f"权限拒绝：工具 {fn_name} 的执行被拒绝。"),
                        }
                        messages.append(denied_msg)
                        session_messages.append(denied_msg)
                        continue
                    res = result_by_idx.get(idx)
                    if res is None:
                        missed_msg = {
                            "role": "tool",
                            "tool_call_id": fid,
                            "_tool_name": fn_name,
                            "_ts": time.time(),
                            "content": f"工具 {fn_name} 未执行（内部调度异常）。",
                        }
                        messages.append(missed_msg)
                        session_messages.append(missed_msg)
                        continue
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": res.tool_call_id,
                        "_tool_name": fn_name,
                        "_ts": time.time(),
                        "content": self._truncate_tool_result(fn_name, res.content or "", res.tool_call_id),
                    }
                    messages.append(tool_msg)
                    session_messages.append(tool_msg)

            else:
                rlog.warning("loop ended | reason=max_turns steps=%s", step)

        except Exception as exc:
            self._hooks.emit(
                "on_error",
                {
                    "stage": stage,
                    "error": exc,
                    "step": step,
                    "request_id": rid,
                    "agent_config": agent_config,
                },
                logger=rlog,
            )
            rlog.error("loop error | step=%s err=%s", step, truncate(str(exc), 400))
            tw.finish(ok=False, final_text=last_content, error=str(exc))
            raise

        if not final_assistant_recorded and last_content:
            # max_turns / doom-loop 等路径下，保证本轮有一个最终 assistant 文本落库
            final_msg = {"role": "assistant", "content": last_content}
            session_messages.append(final_msg)

        tw.finish(ok=True, final_text=last_content, error=None)
        yield DoneEvent(
            text=last_content or "（无回复）",
            steps=step,
            session_messages=session_messages,
        )

    # ------------------------------------------------------------------ #
    #                  run_async：消费 run_stream 的回调包装               #
    # ------------------------------------------------------------------ #
    async def run_async(
        self,
        user_query: str,
        *,
        agent_config: Optional[AgentConfig] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        parent_context: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool_start: Optional[Callable[[str, Dict], None]] = None,
        on_tool_end: Optional[Callable[[str, str, float], None]] = None,
        on_compaction: Optional[Callable[[int, int], None]] = None,
        extra_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        异步主循环（回调风格包装）。消费 run_stream() 并触发各回调。
        返回最终 LLM 回复文本。
        """
        final_text = ""
        async for event in self.run_stream(
            user_query,
            agent_config=agent_config,
            system_prompt=system_prompt,
            model=model,
            parent_context=parent_context,
            request_id=request_id,
            extra_messages=extra_messages,
        ):
            if isinstance(event, TextDeltaEvent):
                if on_text:
                    on_text(event.text)
            elif isinstance(event, ToolStartEvent):
                if on_tool_start:
                    on_tool_start(event.name, event.args)
            elif isinstance(event, ToolEndEvent):
                if on_tool_end:
                    on_tool_end(event.name, event.content, event.elapsed)
            elif isinstance(event, CompactionEvent):
                if on_compaction:
                    on_compaction(event.before_tokens, event.after_tokens)
            elif isinstance(event, DoneEvent):
                final_text = event.text
        return final_text

    async def run_async_with_messages(
        self,
        user_query: str,
        *,
        agent_config: Optional[AgentConfig] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        parent_context: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool_start: Optional[Callable[[str, Dict], None]] = None,
        on_tool_end: Optional[Callable[[str, str, float], None]] = None,
        on_compaction: Optional[Callable[[int, int], None]] = None,
        extra_messages: Optional[List[Dict[str, Any]]] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> tuple[str, List[Dict[str, Any]]]:
        """
        run_async 的扩展版：除 final_text 外，额外返回本轮会话消息增量。
        供 QueryEngine 持久化 tool_calls/tool 轨迹。
        """
        final_text = ""
        session_messages: List[Dict[str, Any]] = []
        async for event in self.run_stream(
            user_query,
            agent_config=agent_config,
            system_prompt=system_prompt,
            model=model,
            parent_context=parent_context,
            request_id=request_id,
            extra_messages=extra_messages,
            abort_event=abort_event,
        ):
            if isinstance(event, TextDeltaEvent):
                if on_text:
                    on_text(event.text)
            elif isinstance(event, ToolStartEvent):
                if on_tool_start:
                    on_tool_start(event.name, event.args)
            elif isinstance(event, ToolEndEvent):
                if on_tool_end:
                    on_tool_end(event.name, event.content, event.elapsed)
            elif isinstance(event, CompactionEvent):
                if on_compaction:
                    on_compaction(event.before_tokens, event.after_tokens)
            elif isinstance(event, DoneEvent):
                final_text = event.text
                if isinstance(event.session_messages, list):
                    session_messages = event.session_messages
        return final_text, session_messages

    # ------------------------------------------------------------------ #
    #                       同步包装                                       #
    # ------------------------------------------------------------------ #
    def run(
        self,
        user_query: str,
        *,
        agent_config: Optional[AgentConfig] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        parent_context: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool_start: Optional[Callable[[str, Dict], None]] = None,
        on_tool_end: Optional[Callable[[str, str, float], None]] = None,
        on_compaction: Optional[Callable[[int, int], None]] = None,
        extra_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        同步包装：在当前线程运行异步主循环。

        如果已有事件循环在运行（如在 Jupyter 中），使用 run_coroutine_threadsafe；
        否则新建 event loop。
        """
        coro = self.run_async(
            user_query,
            agent_config=agent_config,
            system_prompt=system_prompt,
            model=model,
            parent_context=parent_context,
            request_id=request_id,
            on_text=on_text,
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
            on_compaction=on_compaction,
            extra_messages=extra_messages,
        )
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在已有 loop 中（如 Jupyter），使用线程安全方式
                import threading
                result_container = []
                exc_container = []

                def _run_in_new_loop():
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    try:
                        result_container.append(new_loop.run_until_complete(coro))
                    except Exception as e:
                        exc_container.append(e)
                    finally:
                        new_loop.close()

                t = threading.Thread(target=_run_in_new_loop)
                t.start()
                t.join()
                if exc_container:
                    raise exc_container[0]
                return result_container[0] if result_container else "（无回复）"
            else:
                return loop.run_until_complete(coro)
        except RuntimeError:
            # 无可用 loop，创建新的
            return asyncio.run(coro)

    def run_with_messages(
        self,
        user_query: str,
        *,
        agent_config: Optional[AgentConfig] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        parent_context: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool_start: Optional[Callable[[str, Dict], None]] = None,
        on_tool_end: Optional[Callable[[str, str, float], None]] = None,
        on_compaction: Optional[Callable[[int, int], None]] = None,
        extra_messages: Optional[List[Dict[str, Any]]] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> tuple[str, List[Dict[str, Any]]]:
        """
        run 的扩展版：返回 (final_text, session_messages_delta)。
        QueryEngine 使用该接口持久化完整轨迹。
        """
        coro = self.run_async_with_messages(
            user_query,
            agent_config=agent_config,
            system_prompt=system_prompt,
            model=model,
            parent_context=parent_context,
            request_id=request_id,
            on_text=on_text,
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
            on_compaction=on_compaction,
            extra_messages=extra_messages,
            abort_event=abort_event,
        )
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import threading
                result_container: List[tuple[str, List[Dict[str, Any]]]] = []
                exc_container = []

                def _run_in_new_loop():
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    try:
                        result_container.append(new_loop.run_until_complete(coro))
                    except Exception as e:
                        exc_container.append(e)
                    finally:
                        new_loop.close()

                t = threading.Thread(target=_run_in_new_loop)
                t.start()
                t.join()
                if exc_container:
                    raise exc_container[0]
                return result_container[0] if result_container else ("（无回复）", [])
            else:
                return loop.run_until_complete(coro)
        except RuntimeError:
            return asyncio.run(coro)

    # ------------------------------------------------------------------ #
    #                       内部工具方法                                   #
    # ------------------------------------------------------------------ #
    def _get_tools(self, agent_config: AgentConfig) -> List[Dict[str, Any]]:
        """从 ToolRegistry（内建工具）和 MCPClient（MCP 工具）汇合工具列表。"""
        tools: List[Dict[str, Any]] = []

        # 内建工具：由 ToolRegistry 按 agent_config 过滤（读 allow_write_tools 等字段）
        if self._registry is not None:
            try:
                builtin_tools = self._registry.schemas(agent_config=agent_config)
                tools.extend(builtin_tools)
            except Exception as exc:
                self._logger.warning("get builtin tools failed | err=%s", str(exc))

        # MCP 工具：按 allowed_mcp_servers 三态过滤
        #   None       → 全部 server 可见
        #   []         → 完全禁用（等价于原 allow_mcp_tools=False）
        #   [x, y, …]  → 仅白名单 server
        allowed_mcp = agent_config.allowed_mcp_servers
        mcp_enabled = allowed_mcp != []  # None 或非空列表都算启用
        if mcp_enabled and self._mcp_client is not None:
            try:
                mcp_tools = self._mcp_client.list_tools(
                    servers=allowed_mcp,  # None 时 list_tools 自行返回全量
                )
                if not agent_config.allow_write_tools and hasattr(self._mcp_client, "is_read_only"):
                    filtered: List[Dict[str, Any]] = []
                    for schema in mcp_tools:
                        name = (schema.get("function") or {}).get("name", "")
                        if not name:
                            continue
                        if self._mcp_client.is_read_only(name):
                            filtered.append(schema)
                    mcp_tools = filtered
                for schema in mcp_tools:
                    tools.append({
                        "type": schema.get("type", "function"),
                        "function": schema.get("function", {}),
                    })
            except Exception as exc:
                self._logger.warning("get mcp tools failed | err=%s", str(exc))

        # 按 agent 配置禁用子 Agent 工具（对齐 allow_subagent）
        if not agent_config.allow_subagent:
            tools = [
                t for t in tools
                if (t.get("function") or {}).get("name") != "agent"
            ]

        return tools

    def _check_permissions(
        self,
        tool_calls: List[Dict[str, Any]],
        agent_config: AgentConfig,
    ) -> tuple[set[int], Dict[int, str]]:
        """
        检查工具权限。

        Returns:
            (denied_idx_set, deny_reasons)
            - denied_idx_set: 被拒绝的 tool_call 索引
            - deny_reasons:   {idx: reason_str}（供构建 LLM 可读的拒绝消息）
        """
        from whalefall.permissions.manager import PermissionLevel
        denied: set[int] = set()
        deny_reasons: Dict[int, str] = {}

        for idx, tc in enumerate(tool_calls):
            fn_name = tc.get("function", {}).get("name", "")
            # 运行时优先查注册表的 BuiltinTool.read_only；查不到再回退静态 WRITE_TOOLS。
            registry_verdict: Optional[bool] = None
            if self._registry is not None:
                try:
                    registry_verdict = self._registry.is_write_tool_by_name(fn_name)
                except Exception:
                    registry_verdict = None
            mcp_destructive = False
            if self._mcp_client is not None and hasattr(self._mcp_client, "is_destructive"):
                try:
                    mcp_destructive = bool(self._mcp_client.is_destructive(fn_name))
                except Exception:
                    mcp_destructive = False
            if registry_verdict is not None:
                write_like = bool(registry_verdict) or bool(mcp_destructive)
            else:
                write_like = is_write_tool(fn_name) or bool(mcp_destructive)

            # 只读 agent（allow_write_tools=false）拒绝所有写工具
            if not agent_config.allow_write_tools and write_like:
                denied.add(idx)
                deny_reasons[idx] = (
                    f"当前 Agent（{agent_config.name}）不允许写工具 {fn_name}。"
                )
                self._logger.info(
                    "write tool denied for %s agent | tool=%s",
                    agent_config.name, fn_name,
                )
                continue

            # 调用权限管理器（含 BashGuard + 路径约束）
            if self._perm_manager is not None:
                args_str = tc.get("function", {}).get("arguments", "{}")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else {}
                except Exception:
                    args = {}
                level = self._perm_manager.check(fn_name, args, force_write=write_like)
                if level == PermissionLevel.DENY:
                    denied.add(idx)
                    reason = self._perm_manager.deny_reason()
                    deny_reasons[idx] = f"工具 {fn_name} 被安全策略拒绝：{reason}"
                    self._logger.warning("tool DENY | tool=%s reason=%s", fn_name, reason)
                elif level == PermissionLevel.ASK:
                    allowed = self._perm_manager.ask_user(fn_name, args)
                    if not allowed:
                        denied.add(idx)
                        deny_reasons[idx] = f"工具 {fn_name} 的执行被用户拒绝。"

        return denied, deny_reasons

    def _get_tool_max_result_chars(self, tool_name: str) -> int:
        """按工具来源解析结果截断上限。0 表示不截断。"""
        if self._registry is not None:
            builtin = self._registry.get_builtin(tool_name)
            if builtin is not None:
                try:
                    return int(getattr(builtin, "max_result_chars", DEFAULT_MAX_TOOL_RESULT_CHARS))
                except Exception:
                    return DEFAULT_MAX_TOOL_RESULT_CHARS
        if self._mcp_client is not None and hasattr(self._mcp_client, "get_tool_max_result_chars"):
            try:
                return int(self._mcp_client.get_tool_max_result_chars(
                    tool_name, default=DEFAULT_MAX_TOOL_RESULT_CHARS
                ))
            except Exception:
                return DEFAULT_MAX_TOOL_RESULT_CHARS
        return DEFAULT_MAX_TOOL_RESULT_CHARS

    def _truncate_tool_result(self, tool_name: str, content: str, tool_call_id: str = "") -> str:
        max_chars = max(0, self._get_tool_max_result_chars(tool_name))
        if max_chars == 0:
            return content
        if len(content) <= max_chars:
            return content
        # 超限：写完整内容到文件，消息里给路径+预览
        file_path: str = ""
        if tool_call_id:
            try:
                from whalefall.core.runtime import tool_results_dir
                dest = tool_results_dir() / f"{tool_call_id}.txt"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
                file_path = str(dest)
            except Exception:
                pass
        if file_path:
            preview = content[:max_chars]
            return f"[结果过大已截断] 完整内容: {file_path}\n\n{preview}\n..."
        return content[:max_chars] + "\n[...结果已截断]"

    @staticmethod
    def _insert_after_last_system(messages: List[Dict[str, Any]], message: Dict[str, Any]) -> None:
        """把 message 插到最后一个 system 消息之后；若无 system，则插到开头。"""
        insert_at = next(
            (i + 1 for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "system"),
            0,
        )
        messages.insert(insert_at, message)

    @staticmethod
    def _with_tool_call_args(tc: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        """返回一个替换 function.arguments 后的新 tool_call 对象。"""
        new_tc = dict(tc or {})
        fn = dict(new_tc.get("function") or {})
        fn["arguments"] = json.dumps(args or {}, ensure_ascii=False)
        new_tc["function"] = fn
        return new_tc

    def _build_recent_reads_restore_message(self, ctx) -> str:
        """
        压缩后恢复最近读取文件的关键内容，减少重复 read 调用。
        仅恢复最新 N 个文件，且总字符数受限。
        """
        recent = ctx.recently_read or []
        if not recent:
            return ""

        selected = recent[-MAX_RECENTLY_RESTORED_FILES:]
        budget = MAX_RECENTLY_RESTORED_CHARS
        blocks: List[str] = []
        for entry in reversed(selected):
            path = str(entry.get("path", ""))
            content = str(entry.get("content", ""))
            if not path or not content or budget <= 0:
                continue
            head = f"\n--- {path} ---\n"
            room = max(0, budget - len(head))
            if room <= 0:
                break
            snippet = content[:room]
            blocks.append(head + snippet)
            budget -= len(head) + len(snippet)

        if not blocks:
            return ""
        blocks.reverse()
        return (
            "[压缩后文件上下文恢复]\n"
            "以下为最近读取文件的关键片段（自动恢复，用于延续上下文）：\n"
            + "".join(blocks)
        )

    def _build_invoked_skills_restore_message(self, ctx) -> str:
        """
        压缩后恢复已调用过的 skill 全文片段，避免技能上下文丢失。
        """
        invoked = ctx.invoked_skills or []
        if not invoked:
            return ""

        selected = invoked[-MAX_INVOKED_SKILLS_RESTORED:]
        budget = MAX_INVOKED_SKILLS_CHARS
        blocks: List[str] = []

        for entry in reversed(selected):
            name = str(entry.get("name", "")).strip()
            path = str(entry.get("path", "")).strip()
            content = str(entry.get("content", "")).strip()
            if not name or not content or budget <= 0:
                continue
            head = f"\n--- skill: {name} ({path}) ---\n"
            room = max(0, budget - len(head))
            if room <= 0:
                break
            snippet = content[:room]
            blocks.append(head + snippet)
            budget -= len(head) + len(snippet)

        if not blocks:
            return ""
        blocks.reverse()
        return (
            "[压缩后 Skill 上下文恢复]\n"
            "以下为本轮前已加载并使用过的 skill 关键内容（自动恢复）：\n"
            + "".join(blocks)
        )

    @staticmethod
    def _build_pending_tasks_reminder(ctx) -> str:
        """
        新会话启动时检查持久化的未完成任务。
        有活跃任务时注入提醒，让 LLM 知道上次未完成的工作。
        """
        try:
            from whalefall.tools.todo import get_task_store, render_task_list, render_summary
            store = get_task_store(ctx)
            active = store.active_tasks()
            if not active:
                return ""
            return (
                "[系统提醒｜未完成任务]\n"
                "以下是之前会话遗留的未完成任务，请根据用户当前请求决定是否继续：\n\n"
                + render_summary(store) + "\n\n"
                + render_task_list(active, store)
            )
        except Exception:
            return ""

    @staticmethod
    def _build_todo_restore_message(ctx) -> str:
        """
        压缩后恢复任务列表，避免 LLM 忘记当前进行中的任务。
        仅在有未完成任务时才注入。
        """
        store = ctx.metadata.get("_task_store")
        if store is None:
            return ""
        active = store.active_tasks()
        if not active:
            return ""

        from whalefall.tools.todo import render_task_list, render_summary
        return (
            "[压缩后任务列表恢复]\n"
            "以下为当前未完成的任务（自动恢复，请继续执行）：\n"
            + render_summary(store) + "\n\n"
            + render_task_list(active, store)
        )
