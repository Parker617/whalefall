# coding: utf-8
"""
MCP server 入口。
启动方式：
  python -m whalefall.mcp.server          # stdio（默认，供 MCPClient 连接）
  python -m whalefall.mcp.server --sse    # SSE HTTP 服务，端口 8000
"""
import argparse
from whalefall.mcp.server.app import mcp

parser = argparse.ArgumentParser()
parser.add_argument("--sse", action="store_true", help="以 SSE 模式启动（HTTP 服务）")
parser.add_argument("--port", type=int, default=8000)
args = parser.parse_args()

if args.sse:
    mcp.run(transport="sse", port=args.port)
else:
    mcp.run(transport="stdio")
