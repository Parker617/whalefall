# coding: utf-8
"""
示例 MCP 插件：三个极简工具，演示 FastMCP 的自注册机制。

- echo：原样返回输入
- add：两个数相加
- time_now：返回当前本地时间（ISO 格式）

这是"占位"演示，用来跑通整条链路（LLM → MCPClient → FastMCP server → tool）。
真实项目里请在 plugins/ 下新增自己的模块。
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from whalefall.mcp.server.app import mcp


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def echo(
    text: Annotated[str, Field(description="要原样返回的文本")],
) -> str:
    """原样回显传入的文本，用于最小链路验证。"""
    return json.dumps({"echo": text}, ensure_ascii=False)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def add(
    a: Annotated[float, Field(description="第一个加数")],
    b: Annotated[float, Field(description="第二个加数")],
) -> str:
    """两个数相加，返回 JSON 字符串 `{"sum": a + b}`。"""
    return json.dumps({"sum": a + b}, ensure_ascii=False)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def time_now() -> str:
    """返回当前本地时间（ISO 8601，不含毫秒）。"""
    return json.dumps(
        {"now": _dt.datetime.now().replace(microsecond=0).isoformat()},
        ensure_ascii=False,
    )
