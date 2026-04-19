"""
ContextManager：三层 Context 压缩（全异步）。

策略（优先级从低到高）：
1. microcompact  — 截断旧工具结果，不调 LLM（始终执行）
2. autocompact   — token 超阈值时用 LLM 异步生成摘要替换旧消息
3. hard_limit    — 强制截断兜底

- check_and_compact 为 async def，避免阻塞 asyncio 事件循环
- circuit breaker：连续 3 次压缩失败后停止尝试
- last_compacted 标志：供 AgentLoop 决定是否恢复最近读取的文件
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional

from whalefall.core.log import get_logger, truncate

logger = get_logger("whalefall.compaction")

# ── 默认阈值 ────────────────────────────────────────────────────────────────
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
MICRO_TOOL_MAX_CHARS  = 8_000   # microcompact：旧工具结果截断长度
RECENT_PROTECT_ROUNDS = 3       # microcompact：最近 N 轮不截断
KEEP_RECENT_FOR_COMPACT = 6     # autocompact：保留最近 N 轮完整消息
AUTO_COMPACT_RATIO = 0.85       # autocompact 触发比例
HARD_LIMIT_RATIO   = 0.95       # hard limit 比例
COMPACT_FAIL_LIMIT = 3          # circuit breaker：连续失败上限
# 时间戳阈值：超过此分钟数的旧工具结果清除内容（对齐 CC 的 TIME_BASED_MC）
TIME_BASED_MC_MINUTES = 30

# ── microcompact 白名单（仅对这些"重型"工具截断结果）─────────────────────────
# 对齐 CC 的 COMPACTABLE_TOOLS；轻型工具结果完整保留，避免截断 LLM 决策依赖的上下文
COMPACTABLE_TOOLS: frozenset[str] = frozenset({
    "read", "write", "edit", "bash",
    "glob", "grep",
    "web_fetch", "web_search", "web_browser",
    "notebook_edit",
})

# ── 压缩 Prompt ─────────────────────────────────────────────────────────────
_COMPACT_SYSTEM_PROMPT = """\
你是对话上下文摘要助手。
CRITICAL: 只输出文本，不要调用任何工具，不要输出代码块。

按以下格式输出（保留 XML 标签）：

<analysis>
[此处是你的思考过程，不会出现在最终摘要中]
</analysis>

<summary>

## 1. 用户意图
[用户的核心需求和最终目标]

## 2. 已完成工作
[已执行的分析、操作和工具调用，及其结论]

## 3. 当前状态
[进行到哪一步，有哪些待确认问题]

## 4. 涉及文件与数据
[读取/修改的文件路径、关键标识符、数据集名称]

## 5. 工具调用结果摘要
[重要工具结果的核心信息]

## 6. 发现的问题/异常
[错误、异常、边界条件]

## 7. 关键结论
[重要数字、分析结论、决策依据]

## 8. 待办事项
[未完成的步骤]

## 9. 下一步行动
[根据上下文推断的下一步操作]

</summary>

<facts>
{"files_modified": [...文件路径列表...], "decisions": [...关键决策...], "blockers": [...阻碍...], "next_steps": [...下一步...]}
</facts>"""


def _extract_summary(raw: str) -> str:
    """提取 <summary>...</summary> 块；找不到则返回原文。"""
    m = re.search(r"<summary>(.*?)</summary>", raw, re.DOTALL)
    return m.group(1).strip() if m else raw.strip()



class ContextManager:
    """三层 Context 压缩管理器（全异步）。"""

    def __init__(
        self,
        context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
        micro_tool_max_chars: int = MICRO_TOOL_MAX_CHARS,
        recent_protect_rounds: int = RECENT_PROTECT_ROUNDS,
        keep_recent_for_compact: int = KEEP_RECENT_FOR_COMPACT,
        auto_compact_ratio: float = AUTO_COMPACT_RATIO,
        hard_limit_ratio: float = HARD_LIMIT_RATIO,
    ):
        self._auto_thresh = int(context_window_tokens * auto_compact_ratio)
        self._hard_limit  = int(context_window_tokens * hard_limit_ratio)
        self._micro_max        = micro_tool_max_chars
        self._protect_rounds   = recent_protect_rounds
        self._keep_recent      = keep_recent_for_compact
        self._compact_fail_count: int = 0   # circuit breaker 计数
        self.last_compacted: bool = False   # AgentLoop 读取此标志决定是否恢复文件
        self._logger = logger
        self._logger.info(
            "ContextManager init | window=%dk auto_thresh=%dk hard_limit=%dk",
            context_window_tokens // 1000,
            self._auto_thresh // 1000,
            self._hard_limit // 1000,
        )

    # ── 公共入口（async）───────────────────────────────────────────────────

    async def check_and_compact(
        self,
        messages: List[Dict[str, Any]],
        llm_client=None,
        model: Optional[str] = None,
        on_compaction=None,
    ) -> List[Dict[str, Any]]:
        """
        异步检查并执行压缩。每次调用重置 last_compacted。

        Args:
            messages:      当前消息列表
            llm_client:    LLMClient（autocompact 需要）
            model:         模型名（autocompact 使用）
            on_compaction: 回调 (before_tokens, after_tokens)

        Returns:
            压缩后的消息列表（原列表不修改）。
        """
        self.last_compacted = False
        count = self._make_counter(llm_client)
        before_tokens = count(messages)

        # Step 1: microcompact（始终执行，同步，无 LLM 调用）
        messages = self.microcompact(messages)

        # Step 2: autocompact（超阈值且 circuit breaker 未触发）
        after_micro = count(messages)
        if after_micro > self._auto_thresh and llm_client is not None:
            if self._compact_fail_count >= COMPACT_FAIL_LIMIT:
                self._logger.warning(
                    "autocompact skipped (circuit breaker: %d consecutive failures)",
                    self._compact_fail_count,
                )
            else:
                self._logger.info(
                    "autocompact triggered | before=%d after_micro=%d tokens",
                    before_tokens, after_micro,
                )
                try:
                    messages = await self.autocompact(messages, llm_client, model)
                    self._compact_fail_count = 0
                    self.last_compacted = True
                except Exception as exc:
                    self._compact_fail_count += 1
                    self._logger.warning(
                        "autocompact failed [%d/%d] | err=%s",
                        self._compact_fail_count, COMPACT_FAIL_LIMIT,
                        truncate(str(exc), 200),
                    )

        # Step 3: hard_limit（兜底）
        current = count(messages)
        if current > self._hard_limit:
            self._logger.warning(
                "hard_limit triggered | tokens=%d limit=%d", current, self._hard_limit
            )
            messages = self._hard_truncate(messages, count)
            self.last_compacted = True

        after_tokens = count(messages)
        if self.last_compacted:
            self._logger.info(
                "compaction done | before=%d after=%d ratio=%.2f",
                before_tokens, after_tokens, after_tokens / max(before_tokens, 1),
            )
            if on_compaction is not None:
                try:
                    on_compaction(before_tokens, after_tokens)
                except Exception:
                    pass

        return messages

    # ── microcompact ───────────────────────────────────────────────────────

    def microcompact(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        轻量增量截断（对齐 CC 的 microCompact）：

        策略：
        1. 保留最近 RECENT_PROTECT_ROUNDS 轮完整不动
        2. 超出范围的旧轮次：
           a) 仅对 COMPACTABLE_TOOLS 白名单工具的结果截断
              （轻型工具如 todo_write/skill/agent 结果完整保留）
           b) 时间标记截断（TIME_BASED_MC）：携带 _ts 元数据且超过阈值的结果清空
           c) 通过 assistant.tool_calls 建立 tool_call_id -> tool_name 映射，
              避免会话持久化后 tool 消息缺失 name 导致“全量误截断”
        """
        import time
        if not messages:
            return messages

        now = time.time()
        age_threshold_sec = TIME_BASED_MC_MINUTES * 60
        tool_name_by_id: Dict[str, str] = {}

        # 找出每轮 assistant+tool_calls 的起始位置
        round_starts = [
            i for i, msg in enumerate(messages)
            if msg.get("role") == "assistant" and msg.get("tool_calls")
        ]

        if len(round_starts) <= self._protect_rounds:
            return messages

        protected_from = round_starts[-self._protect_rounds]
        result = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in (msg.get("tool_calls") or []):
                    if not isinstance(tc, dict):
                        continue
                    tid = str(tc.get("id", "")).strip()
                    fn = tc.get("function") or {}
                    name = str(fn.get("name", "")).strip() if isinstance(fn, dict) else ""
                    if tid and name:
                        tool_name_by_id[tid] = name

            if i >= protected_from:
                result.append(msg)
            elif msg.get("role") == "tool":
                content = msg.get("content") or ""
                tool_call_id = str(msg.get("tool_call_id") or "").strip()
                tool_name = str(
                    msg.get("name")
                    or msg.get("_tool_name")
                    or tool_name_by_id.get(tool_call_id, "")
                )
                ts = msg.get("_ts")  # 工具执行时间戳（AgentLoop 回填 tool 消息时写入）

                # TIME_BASED_MC: 超时旧结果直接清空（节省 token）
                if ts and isinstance(ts, (int, float)) and (now - ts) > age_threshold_sec:
                    msg = {**msg, "content": "[旧工具结果内容已清除（超过时间阈值）]"}
                    result.append(msg)
                    continue

                # COMPACTABLE_TOOLS 白名单：只截断重型工具结果。
                # 无法识别工具名时保守处理（不截断），避免误伤轻量工具上下文。
                if tool_name in COMPACTABLE_TOOLS:
                    if isinstance(content, str) and len(content) > self._micro_max:
                        msg = {**msg, "content": content[:self._micro_max] + "\n[...已截断...]"}
                result.append(msg)
            else:
                result.append(msg)
        return result

    # ── autocompact ────────────────────────────────────────────────────────

    async def autocompact(
        self,
        messages: List[Dict[str, Any]],
        llm_client,
        model: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """异步：用 LLM 生成摘要替换早期历史，返回压缩后的消息列表。"""
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system  = [m for m in messages if m.get("role") != "system"]

        turn_starts = [
            i for i, m in enumerate(non_system)
            if m.get("role") in ("user", "assistant") and (
                m.get("role") == "user" or m.get("tool_calls")
            )
        ]
        if len(turn_starts) <= self._keep_recent:
            return messages

        keep_from    = turn_starts[-self._keep_recent]
        to_summarize = non_system[:keep_from]
        to_keep      = non_system[keep_from:]

        if not to_summarize:
            return messages

        summary = await self._call_llm_for_summary(llm_client, model, to_summarize)
        new_messages = (
            system_msgs
            + [{
                "role": "user",
                "content": (
                    "[对话历史摘要 - 系统自动生成]\n\n"
                    + summary
                    + "\n\n[以上为历史摘要，以下为最近对话]"
                ),
            }]
            + to_keep
        )
        self._logger.info(
            "autocompact done | from=%d msgs to=%d msgs summary_chars=%d",
            len(messages), len(new_messages), len(summary),
        )
        return new_messages

    async def _call_llm_for_summary(
        self,
        llm_client,
        model: Optional[str],
        to_summarize: List[Dict[str, Any]],
    ) -> str:
        """异步调 LLM 生成摘要；prompt_too_long 时截半重试一次。"""
        msgs_to_use = list(to_summarize)
        for attempt in range(2):
            history_text = self._messages_to_text(msgs_to_use)
            self._logger.info(
                "calling LLM for summary | attempt=%d history_chars=%d",
                attempt + 1, len(history_text),
            )
            try:
                raw = await llm_client.call_llm_async(
                    prompt=history_text,
                    model=model,
                    system_message=_COMPACT_SYSTEM_PROMPT,
                    timeout=120,
                    max_tokens=3000,
                )
                summary = _extract_summary(raw or "")
                if not summary:
                    raise RuntimeError("LLM 返回空摘要")
                return summary
            except Exception as exc:
                err_str = str(exc).lower()
                if attempt == 0 and any(k in err_str for k in ("too long", "context", "tokens", "limit")):
                    cutoff = len(msgs_to_use) // 2
                    msgs_to_use = msgs_to_use[cutoff:]
                    self._logger.info("prompt_too_long: retrying with %d messages", len(msgs_to_use))
                    continue
                raise

        raise RuntimeError("autocompact: 重试后仍失败")

    # ── hard_limit ─────────────────────────────────────────────────────────

    def _hard_truncate(
        self,
        messages: List[Dict[str, Any]],
        count,
    ) -> List[Dict[str, Any]]:
        """强制截断：保留 system + 最近若干消息，总 token < hard_limit * 0.8。"""
        target = int(self._hard_limit * 0.8)
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system  = [m for m in messages if m.get("role") != "system"]

        kept = []
        acc  = count(system_msgs)
        for msg in reversed(non_system):
            cost = count([msg])
            if acc + cost > target and kept:
                break
            kept.append(msg)
            acc += cost

        kept.reverse()
        result = system_msgs + kept
        self._logger.warning(
            "hard_truncate: kept %d/%d messages", len(result), len(messages)
        )
        return result

    # ── 工具方法 ───────────────────────────────────────────────────────────

    def _make_counter(self, llm_client=None):
        """返回 messages → token 估计函数。有 tiktoken 用 tiktoken，否则 chars//4。"""
        if llm_client is not None and hasattr(llm_client, "count_tokens"):
            def _count(msgs):
                return sum(llm_client.count_tokens(str(m)) for m in msgs)
            return _count
        def _count_chars(msgs):
            return sum(len(str(m)) for m in msgs) // 4
        return _count_chars

    def estimate_tokens(self, messages: List[Dict[str, Any]], llm_client=None) -> int:
        return self._make_counter(llm_client)(messages)

    def _messages_to_text(self, messages: List[Dict[str, Any]]) -> str:
        """将消息列表转为便于 LLM 摘要的文本。"""
        lines: List[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls")

            if role == "assistant":
                if content:
                    lines.append(f"[助手]: {content}")
                for tc in (tool_calls or []):
                    fn = tc.get("function") or {}
                    lines.append(f"[工具调用]: {fn.get('name', '')}({truncate(fn.get('arguments', ''), 300)})")
            elif role == "tool":
                lines.append(f"[工具结果]: {truncate(str(content), 500)}")
            elif role == "user":
                lines.append(f"[用户]: {content}")
            elif role == "system":
                lines.append(f"[系统]: {truncate(str(content), 200)}")

        return "\n".join(lines)
