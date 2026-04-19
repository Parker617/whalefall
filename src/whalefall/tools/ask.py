# coding: utf-8
"""
AskUserQuestionTool：让 Agent 向用户提问，等待用户回答后继续执行。

对齐 CC 的 AskUserQuestionTool 行为：
- LLM 在需要澄清时调用本工具（而非直接回复）
- 在 CLI 环境中通过 input() 获取用户输入
- 支持通过 ctx.ask_user_callback 注入自定义输入（Web UI / 测试场景）
"""
from __future__ import annotations

import sys
from typing import Any, Dict

from whalefall.tools.base import BuiltinTool, ToolContext


class AskUserQuestionTool(BuiltinTool):
    """暂停执行，向用户提出问题并等待回答。"""

    name = "ask_user_question"
    description = (
        "向用户提出一个需要澄清的问题，暂停当前任务等待用户回答。\n"
        "仅在任务方向不明确、缺少关键信息、或继续执行风险较大时使用。\n"
        "不要为了确认已知信息而频繁提问。"
    )
    read_only = True
    max_result_chars = 4_000

    parameters_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "向用户提出的问题（简洁、具体）",
            },
        },
        "required": ["question"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        question = str(args.get("question", "")).strip()
        if not question:
            return "错误：question 参数不能为空"

        # 优先使用 ctx 注入的回调（Web UI、测试场景）
        if ctx.ask_user_callback is not None:
            try:
                answer = ctx.ask_user_callback(question)
                return str(answer).strip() if answer is not None else ""
            except Exception as exc:
                return f"获取用户输入失败: {exc}"

        # 回退：CLI stdin
        # 非交互环境（Web 服务、后台进程、CI）避免阻塞在 input()
        if not (hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
            return (
                "[ask_user_question] 当前环境不支持交互输入。"
                "请在下一条用户消息中直接回答以下问题：\n"
                f"{question}"
            )

        try:
            print(f"\n[Agent 提问] {question}")
            answer = input(">>> ").strip()
            return answer
        except (EOFError, KeyboardInterrupt):
            return "[用户未回答或中断]"
