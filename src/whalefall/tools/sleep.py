"""
SleepTool：暂停执行 N 秒。

适用场景：
- 等待外部 API 限速恢复
- 轮询等待文件/任务生成
- 限流保护
"""
from __future__ import annotations

import time
from typing import Any, Dict

from whalefall.tools.base import BuiltinTool, ToolContext


class SleepTool(BuiltinTool):
    name = "sleep"
    description = (
        "暂停执行指定秒数。\n"
        "适用于：等待 API 限速恢复、轮询等待任务完成、限流保护。\n"
        "最大等待 300 秒。"
    )
    read_only = True
    max_result_chars = 200

    parameters_schema = {
        "type": "object",
        "properties": {
            "seconds": {
                "type": "number",
                "description": "暂停秒数（0.1 ~ 300）",
            },
        },
        "required": ["seconds"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        try:
            seconds = float(args.get("seconds", 1))
        except (TypeError, ValueError):
            return "错误：seconds 必须是数字"

        seconds = max(0.1, min(seconds, 300.0))
        time.sleep(seconds)
        return f"已等待 {seconds:.1f} 秒"
