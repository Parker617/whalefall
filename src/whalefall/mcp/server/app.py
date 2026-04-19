# coding: utf-8
"""
FastMCP 实例 + 插件装配。

FastMCP 单例集中在本模块定义，`whalefall.mcp.plugins` 下的插件模块 import 本
模块的 `mcp` 变量并用 `@mcp.tool()` 自注册。

要扩展/关闭某个插件，修改下面的 import 列表即可；无需改动 server 本身。
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("whalefall")

from whalefall.mcp.plugins import hello  # noqa: E402, F401
