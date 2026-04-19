#!/usr/bin/env python3
"""
whalefall CLI 入口

用法：
  python -m whalefall.main                                  # 交互模式
  python -m whalefall.main "列出项目里用到了哪些设计模式"     # 单次执行
  python -m whalefall.main --agent explore "搜索所有 Python 文件"
  python -m whalefall.main --no-stream "..."                # 非流式
  python -m whalefall.main --model gpt-4o "..."             # 指定模型
  python -m whalefall.main --bypass "..."                   # 跳过权限检查
  python -m whalefall.main --agent plan "规划重构方案"
  python -m whalefall.main --agent verify "验证前面分析是否自洽"

选项说明：
  --model MODEL     使用的 LLM 模型（默认: gpt-4o-mini；别名见 llm_config.ini）
  --agent TYPE      Agent 类型（general/explore/plan/verify，默认: general）
  --no-stream       禁用流式输出（等待完整响应）
  --bypass          --dangerously-bypass-permissions（跳过所有权限询问）
  --request-id ID   指定请求 ID（用于 trace）
  --max-turns N     覆盖本次任务最大回合数
  --no-mcp          禁用 MCP 工具（仅用内建工具）
  --no-builtin      禁用内建工具（仅用 MCP 工具）
  --verbose         详细日志（设置 MCP_LOG_STDOUT=1）
"""
from __future__ import annotations

import argparse
import os
import sys


def _parse_args():
    # 动态读取 agent/roles/definitions/ 下注册的全部 agent，避免新增 custom agent
    # 时还要来改 CLI 硬编码。
    from whalefall.agent.roles import list_agent_names
    agent_choices = list_agent_names() or ["general"]

    parser = argparse.ArgumentParser(
        prog="whalefall",
        description="whalefall：本地工具增强型 AI 助手",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "query",
        nargs="?",
        default=None,
        help="查询内容（不提供则进入交互模式）",
    )
    parser.add_argument(
        "--model", "-m",
        default=None,
        help="使用的 LLM 模型（默认: gpt-4o-mini；别名见 llm_config.ini）",
    )
    parser.add_argument(
        "--agent", "-a",
        default="general",
        choices=agent_choices,
        help="Agent 名称（从 agent/roles/definitions/ 动态加载，默认: general）",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="覆盖本次任务最大回合数（仅当前命令生效）",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="禁用流式输出",
    )
    parser.add_argument(
        "--bypass",
        action="store_true",
        help="跳过所有权限检查（危险模式）",
    )
    parser.add_argument(
        "--request-id",
        default=None,
        help="指定请求 ID（用于 trace 追踪）",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="禁用 MCP 工具",
    )
    parser.add_argument(
        "--no-builtin",
        action="store_true",
        help="禁用内建工具",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细日志输出",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="启动 Web UI（浏览器对话界面）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Web UI 端口（默认 8000，仅 --web 时有效）",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Web UI 监听地址（默认 0.0.0.0）",
    )
    return parser.parse_args()


def main():
    args = _parse_args()

    if args.web:
        from whalefall.ui.web import run as web_run
        print(f"Web UI 启动中：http://{args.host if args.host != '0.0.0.0' else 'localhost'}:{args.port}")
        web_run(host=args.host, port=args.port, model=args.model or "gpt-4o-mini")
        return

    # 设置日志级别
    if args.verbose:
        os.environ.setdefault("MCP_LOG_STDOUT", "1")
        os.environ.setdefault("MCP_LOG_LEVEL", "DEBUG")

    # 构建 LLM 客户端
    _model = args.model or "gpt-4o-mini"
    try:
        from whalefall.llm.llm_client import LLMClient
        llm = LLMClient(model=_model)
    except Exception as e:
        print(f"错误：LLM 客户端初始化失败 - {e}", file=sys.stderr)
        sys.exit(1)

    # 读取模型 context window（用于 ContextManager 阈值推导）
    from whalefall.llm.config import get_model_context
    _context_window = get_model_context(_model)

    # 内建工具注册表
    registry = None
    if not args.no_builtin:
        try:
            from whalefall.tools.registry import build_default_registry
            registry = build_default_registry()
            print(f"内建工具已加载: {len(registry)} 个工具")
        except Exception as e:
            print(f"[警告] 内建工具加载失败: {e}", file=sys.stderr)

    # MCP 客户端（工具 schema 由 AgentLoop._get_tools() 通过 mcp_client.list_tools() 获取）
    mcp_client = None
    if not args.no_mcp:
        try:
            from whalefall.mcp import MCPClient
            mcp_client = MCPClient()
            mcp_client.connect()
            print(f"MCP 工具已加载: {len(mcp_client.list_tools())} 个工具")
        except Exception as e:
            print(f"[警告] MCP 连接跳过: {e}", file=sys.stderr)

    # 权限管理器
    from whalefall.permissions.manager import PermissionManager
    if args.bypass:
        perm_manager = PermissionManager.create_bypass()
        print("[警告] 权限检查已禁用（bypass 模式）")
    else:
        perm_manager = PermissionManager(interactive=True)

    # AgentLoop + QueryEngine（会话层）
    from whalefall.agent.loop import AgentLoop
    from whalefall.agent.query_engine import QueryEngine
    from whalefall.agent.hooks import build_default_hook_manager
    loop = AgentLoop(
        llm_client=llm,
        tool_registry=registry,
        mcp_client=mcp_client,
        permission_manager=perm_manager,
        context_window_tokens=_context_window,
        hook_manager=build_default_hook_manager(),
    )
    query_engine = QueryEngine(loop)

    # 单次执行模式
    if args.query:
        from whalefall.ui.cli import InteractiveCLI
        cli = InteractiveCLI(
            agent_loop=loop,
            query_engine=query_engine,
            model=_model,
            agent_type=args.agent,
            stream=not args.no_stream,
            bypass_permissions=args.bypass,
            request_id=args.request_id,
            max_turns=args.max_turns,
        )
        try:
            result = cli.run_once(args.query)
            if args.no_stream:
                print(result)
        finally:
            if mcp_client is not None:
                try:
                    mcp_client.disconnect()
                except Exception:
                    pass
        return

    # 交互模式
    from whalefall.ui.cli import InteractiveCLI
    cli = InteractiveCLI(
        agent_loop=loop,
        query_engine=query_engine,
        model=_model,
        agent_type=args.agent,
        stream=not args.no_stream,
        bypass_permissions=args.bypass,
        request_id=args.request_id,
        max_turns=args.max_turns,
    )
    try:
        cli.start()
    finally:
        # 清理资源
        if mcp_client is not None:
            try:
                mcp_client.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    main()
