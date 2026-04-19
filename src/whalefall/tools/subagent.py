"""
AgentTool：派生子 Agent 的内建工具。

- read_only=True（子 Agent 自己控制写权限）
- 所有 agent（内建 + 自定义）从 agent/roles/definitions/<name>/AGENT.md 动态加载；
  本工具的 description + subagent_type enum 由 list_agent_names() 启动时装配
- 新建独立 AgentLoop 实例递归执行
- max_result_chars=60_000（子 Agent 输出可能较长）
- run_in_background=True：后台异步执行，立即返回 job_id；
  再次调用时传 job_id 获取结果
- finally 块清理子 Agent 资源，防止长会话内存泄漏
- Transcript 保存：子 Agent 完整对话记录持久化到 .runtime/transcripts/
- Agent 级 MCP 过滤：根据 AgentConfig.allowed_mcp_servers 过滤 MCP 工具
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Dict, List

from whalefall.tools.base import BuiltinTool, ToolContext
from whalefall.agent.roles import get_agent, list_agent_names, load_agents

_JOBS_KEY = "_agent_jobs"  # ctx.metadata 中的 key
_MAX_BG_JOBS = 50          # 最多保留后台任务数（含运行中）

# 后台 Agent 单次等待的默认超时（秒）。超时后调用方拿到提示消息，可以用 job_id 继续等。
_BG_JOB_DEFAULT_TIMEOUT = 60.0
_BG_JOB_MAX_TIMEOUT = 1800.0


def _bg_job_default_timeout() -> float:
    raw = (os.getenv("WHALEFALL_AGENT_BG_TIMEOUT") or "").strip()
    if not raw:
        return _BG_JOB_DEFAULT_TIMEOUT
    try:
        v = float(raw)
    except Exception:
        return _BG_JOB_DEFAULT_TIMEOUT
    return max(1.0, min(v, _BG_JOB_MAX_TIMEOUT))


def _get_jobs(ctx: ToolContext) -> Dict[str, Future]:
    if _JOBS_KEY not in ctx.metadata:
        ctx.metadata[_JOBS_KEY] = {}
    return ctx.metadata[_JOBS_KEY]


def _prune_done_jobs(jobs: Dict[str, Future]) -> None:
    """仅清理已完成任务，绝不丢弃运行中任务。"""
    done_ids = [jid for jid, f in jobs.items() if f.done()]
    for jid in done_ids:
        jobs.pop(jid, None)


def _emit_subagent_hook(ctx: ToolContext, agent_config, prompt: str) -> None:
    """触发 subagent_start hook，允许外部注入上下文。"""
    if ctx.hook_manager is None:
        return
    from whalefall.agent.hooks import HOOK_SUBAGENT_START
    ctx.hook_manager.emit(
        HOOK_SUBAGENT_START,
        {
            "agent_type": agent_config.name,
            "prompt": prompt[:500],
            "max_turns": agent_config.max_turns,
            "allow_write": agent_config.allow_write_tools,
        },
    )


def _save_transcript(
    agent_name: str,
    prompt: str,
    final_text: str,
    session_messages: List[Dict[str, Any]],
) -> str:
    """
    保存子 Agent 的完整对话记录到 .runtime/transcripts/。
    返回保存路径（失败返回空字符串）。
    """
    try:
        from whalefall.core.runtime import runtime_root
        transcript_dir = runtime_root() / "transcripts"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{ts}_{agent_name}.json"
        filepath = transcript_dir / filename

        # 清理消息中的不可序列化内容
        clean_messages = []
        for msg in (session_messages or []):
            clean = {}
            for k, v in msg.items():
                try:
                    json.dumps(v, ensure_ascii=False, default=str)
                    clean[k] = v
                except (TypeError, ValueError):
                    clean[k] = str(v)
            clean_messages.append(clean)

        data = {
            "agent_name": agent_name,
            "timestamp": ts,
            "prompt": prompt,
            "final_text": final_text,
            "messages": clean_messages,
        }
        filepath.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return str(filepath)
    except Exception:
        return ""


def _create_filtered_mcp_client(ctx: ToolContext, mcp_servers: List[str]):
    """
    创建一个 MCP 工具过滤包装器：只暴露指定 server 的工具。
    不创建新连接，复用已有 MCPClient 的连接池。
    """
    if ctx.mcp_client is None:
        return None

    class _FilteredMCPClient:
        """轻量包装器，过滤 MCPClient 的工具列表到指定 server。"""

        def __init__(self, inner, servers: List[str]):
            self._inner = inner
            self._servers = servers

        def list_tools(self, servers=None):
            # 取交集：agent 声明的 + 调用方额外过滤的
            effective = self._servers
            if servers:
                effective = [s for s in servers if s in self._servers]
            return self._inner.list_tools(servers=effective)

        def call_tool(self, api_name, arguments):
            return self._inner.call_tool(api_name, arguments)

        def is_read_only(self, api_name):
            return self._inner.is_read_only(api_name)

        def is_destructive(self, api_name):
            return self._inner.is_destructive(api_name)

        def get_tool_max_result_chars(self, api_name, default=12_000):
            return self._inner.get_tool_max_result_chars(api_name, default)

    return _FilteredMCPClient(ctx.mcp_client, mcp_servers)


def _render_agent_catalog() -> tuple[str, list[str]]:
    """
    读 `agent/roles/definitions/*/AGENT.md`，拼成一份 "agent 名称 → 描述/权限" 清单。
    返回 (多行文本块, 所有 agent 名称列表) 供 AgentTool 装配 description + schema。
    """
    agents = load_agents()
    names = sorted(agents.keys())
    lines: list[str] = []
    for name in names:
        cfg = agents[name]
        perms: list[str] = []
        perms.append("可写" if cfg.allow_write_tools else "只读")
        if cfg.allowed_mcp_servers != []:  # None 或非空白名单都算允许
            perms.append("MCP")
        if cfg.allow_subagent:
            perms.append("可再嵌套")
        desc = (cfg.description or "").strip() or "(无描述)"
        lines.append(f"  - {name}（{'/'.join(perms)}，max_turns={cfg.max_turns}）：{desc}")
    return "\n".join(lines), names


class AgentTool(BuiltinTool):
    """派生子 Agent，在独立消息上下文中执行子任务。支持后台运行。"""

    name = "agent"
    read_only = True
    max_result_chars = 60_000

    def __init__(self) -> None:
        super().__init__()
        catalog, names = _render_agent_catalog()
        self.description = (
            "派生一个子 Agent 来执行独立的子任务。子 Agent 有独立的消息历史。\n"
            "subagent_type 可选（从 agent/roles/definitions/ 动态加载）：\n"
            f"{catalog}\n"
            "run_in_background=true 时立即返回 job_id，子 Agent 后台执行；"
            "再次调用时传入 job_id（不传 prompt）可获取结果。\n"
            "返回子 Agent 的最终输出文本。"
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "子 Agent 的任务描述（获取后台结果时可省略）",
                },
                "subagent_type": {
                    "type": "string",
                    "description": "子 Agent 名称；可选值见 description。默认 general",
                    "enum": names or ["general"],
                    "default": "general",
                },
                "model": {
                    "type": "string",
                    "description": "可选：子 Agent 使用的模型（覆盖默认）",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "true = 后台运行，立即返回 job_id；false（默认）= 阻塞等待结果",
                    "default": False,
                },
                "job_id": {
                    "type": "string",
                    "description": "查询后台 Agent 结果时传入的 job_id（由 run_in_background=true 时返回）",
                },
                "wait_seconds": {
                    "type": "number",
                    "description": (
                        "查询后台 job 时的等待上限（秒）。默认读取 WHALEFALL_AGENT_BG_TIMEOUT，"
                        "未设置时为 60。最大 1800。超时后返回\"仍在运行\"并保留 job。"
                    ),
                },
            },
            "required": [],
        }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        job_id = (args.get("job_id") or "").strip()

        # ── 查询后台任务结果 ─────────────────────────────────────────────
        if job_id:
            raw_wait = args.get("wait_seconds", None)
            wait_s: float
            if raw_wait is None:
                wait_s = _bg_job_default_timeout()
            else:
                try:
                    wait_s = float(raw_wait)
                except Exception:
                    wait_s = _bg_job_default_timeout()
                wait_s = max(1.0, min(wait_s, _BG_JOB_MAX_TIMEOUT))
            return self._get_result(job_id, ctx, timeout=wait_s)

        # ── 启动新子 Agent ───────────────────────────────────────────────
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "错误：prompt 或 job_id 参数不能同时为空"

        subagent_type = (args.get("subagent_type") or "general").lower()
        model = args.get("model") or None
        run_in_background = bool(args.get("run_in_background", False))

        # 查找 AgentConfig：统一走 agent.roles loader，找不到回退 general
        agent_config = get_agent(subagent_type)
        if model:
            from dataclasses import replace
            agent_config = replace(agent_config, model=model)

        # 触发 subagent_start hook
        _emit_subagent_hook(ctx, agent_config, prompt)

        # Agent 级 MCP server 三态过滤（None=全部 / []=禁用 / [...]=白名单）
        allowed_mcp = agent_config.allowed_mcp_servers
        if allowed_mcp == []:
            mcp_client = None
        elif allowed_mcp:
            mcp_client = _create_filtered_mcp_client(ctx, allowed_mcp)
        else:
            mcp_client = ctx.mcp_client

        from whalefall.agent.loop import AgentLoop
        sub_loop = AgentLoop(
            llm_client=ctx.llm_client,
            tool_registry=ctx.tool_registry,
            mcp_client=mcp_client,
            permission_manager=ctx.permission_manager,
            hook_manager=ctx.hook_manager,
        )

        if run_in_background:
            return self._run_background(sub_loop, prompt, agent_config, model, ctx)
        else:
            return self._run_foreground(sub_loop, prompt, agent_config, model)

    # ------------------------------------------------------------------ #

    @staticmethod
    def _run_foreground(sub_loop, prompt, agent_config, model) -> str:
        try:
            final_text, session_messages = sub_loop.run_with_messages(
                user_query=prompt,
                agent_config=agent_config,
                model=model,
                request_id=None,
            )
            # 保存 transcript
            _save_transcript(agent_config.name, prompt, final_text, session_messages)
            return final_text
        except RuntimeError as e:
            return f"子 Agent 终止: {e}"
        except Exception as e:
            return f"子 Agent 执行失败: {type(e).__name__}: {e}"
        finally:
            sub_loop.cleanup()

    @staticmethod
    def _run_background(sub_loop, prompt, agent_config, model, ctx: ToolContext) -> str:
        future: Future = Future()
        jid = f"agent-{uuid.uuid4().hex[:8]}"
        jobs = _get_jobs(ctx)
        _prune_done_jobs(jobs)
        if len(jobs) >= _MAX_BG_JOBS:
            running = sum(1 for f in jobs.values() if not f.done())
            return (
                f"错误：后台 Agent 任务过多（运行中 {running}，上限 {_MAX_BG_JOBS}）。\n"
                "请先用已有 job_id 拉取结果，再启动新任务。"
            )
        jobs[jid] = future

        def _worker():
            try:
                final_text, session_messages = sub_loop.run_with_messages(
                    user_query=prompt,
                    agent_config=agent_config,
                    model=model,
                    request_id=None,
                )
                _save_transcript(agent_config.name, prompt, final_text, session_messages)
                if not future.done():
                    future.set_result(final_text)
            except Exception as e:
                if not future.done():
                    future.set_exception(e)
            finally:
                sub_loop.cleanup()

        t = threading.Thread(target=_worker, daemon=True, name=f"agent-bg-{jid}")
        t.start()
        return (
            f"[后台 Agent 已启动]\n"
            f"job_id: {jid}\n"
            f"类型: {agent_config.name}\n"
            f"任务: {prompt[:100]}{'...' if len(prompt) > 100 else ''}\n\n"
            f"使用 agent 工具传入 job_id=\"{jid}\" 获取结果（结果未就绪时会阻塞等待）。"
        )

    @staticmethod
    def _get_result(job_id: str, ctx: ToolContext, *, timeout: float) -> str:
        jobs = _get_jobs(ctx)
        future = jobs.get(job_id)
        if future is None:
            return f"错误：未找到 job_id={job_id!r}，请确认 ID 正确且在同一会话中。"
        from concurrent.futures import TimeoutError as FuturesTimeoutError
        try:
            result = future.result(timeout=max(1.0, float(timeout)))
            jobs.pop(job_id, None)
            return result
        except FuturesTimeoutError:
            return (
                f"后台 Agent 仍在运行（等待 {int(timeout)}s 后返回），job_id={job_id}。\n"
                "请稍后再次用该 job_id 获取结果；可传入 wait_seconds 调整等待。"
            )
        except RuntimeError as e:
            jobs.pop(job_id, None)
            return f"后台 Agent 终止: {e}"
        except Exception as e:
            jobs.pop(job_id, None)
            return f"后台 Agent 执行失败: {type(e).__name__}: {e}"
