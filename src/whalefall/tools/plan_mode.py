# coding: utf-8
"""
EnterPlanModeTool / ExitPlanModeTool：内联规划模式切换。

规划模式下 Agent 只描述计划，不执行工具（tool_calls 转为文字描述）。
对应 CC 的 EnterPlanMode / ExitPlanMode。
"""
from __future__ import annotations

from typing import Any, Dict

from whalefall.tools.base import BuiltinTool, ToolContext


class EnterPlanModeTool(BuiltinTool):
    """进入规划模式：Agent 只输出计划，不执行工具。"""

    name = "enter_plan_mode"
    description = (
        "进入规划模式。在此模式下，你只需描述你的执行计划（步骤、工具调用意图），"
        "不会实际执行任何工具。"
        "该模式仅对当前任务回合生效；若需在本回合恢复执行，可调用 exit_plan_mode。\n"
        "适用场景：任务复杂、影响范围大、或用户要求先看计划再执行时使用。"
    )
    read_only = True
    max_result_chars = 500

    parameters_schema = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "进入规划模式的原因（可选）",
            }
        },
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        ctx.plan_mode = True
        reason = str(args.get("reason", "")).strip()
        msg = (
            "[已进入规划模式]\n"
            "你现在处于只规划、不执行的模式（仅当前任务回合有效）。\n"
            "请描述完整计划（步骤、涉及文件、预期影响）。"
            "若需在本回合恢复执行，请先调用 exit_plan_mode。"
        )
        if reason:
            msg += f"\n原因：{reason}"
        return msg


class ExitPlanModeTool(BuiltinTool):
    """退出规划模式，恢复正常工具执行。"""

    name = "exit_plan_mode"
    description = (
        "退出规划模式，恢复正常执行。"
        "在用户确认计划后调用此工具，然后按计划实际执行各工具。"
    )
    read_only = True
    max_result_chars = 200

    parameters_schema = {"type": "object", "properties": {}}

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        ctx.plan_mode = False
        return "[已退出规划模式] 现在可以按计划执行工具了。"
