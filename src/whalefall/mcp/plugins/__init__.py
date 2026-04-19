"""
whalefall.mcp.plugins —— FastMCP 插件示例。

插件以模块为单位；import 时通过 `@mcp.tool()` 自动注册到
`whalefall.mcp.server.app.mcp` 这一 FastMCP 单例。

目录仅放"插件实现"，FastMCP 服务器启动/挂载逻辑仍由 `mcp/server/app.py` 负责。

默认只有一个演示插件 `hello.py`。要加自己的工具：
  1. 在本目录下新建模块，比如 `my_tools.py`
  2. 在模块里 `from whalefall.mcp.server.app import mcp`
  3. 用 `@mcp.tool()` 装饰函数即可自动注册
  4. 最后在 `whalefall/mcp/server/app.py` 里 `import my_tools`
"""
