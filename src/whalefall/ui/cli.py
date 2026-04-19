"""
InteractiveCLI：交互式命令行界面，使用 Rich 库实现。

特性：
- 流式文本输出（real-time delta）
- 工具调用可视化（调用/完成/失败状态）
- Context 压缩提示
- 特殊命令：/exit /clear /resume /model /agent /compact /init /stats /help
- 非交互模式：run_once()
- token/cost 统计（每轮结束显示）

Rich 优雅降级：如未安装 Rich，退化到纯 print 模式。
"""
from __future__ import annotations

import time
from uuid import uuid4
from typing import Optional

from whalefall.ui.slash import (
    COMMON_HELP_LINES,
    SlashContext,
    dispatch_common,
    parse_slash,
)
from whalefall.ui.streaming import StreamHandler

VERSION = "1.0.0"

# 尝试导入 Rich
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.rule import Rule
    _HAS_RICH = True
    _console = Console()
except ImportError:
    _HAS_RICH = False
    _console = None


def _print(msg: str, style: str = "", end: str = "\n") -> None:
    """统一输出：有 Rich 用 Rich，没有用 print。"""
    if _HAS_RICH and _console:
        try:
            _console.print(msg, style=style, end=end)
        except Exception:
            print(msg, end=end)
    else:
        print(msg, end=end)


class InteractiveCLI:
    """
    交互式 CLI。优先通过 QueryEngine 实现多轮会话，
    无 QueryEngine 时降级为单轮 AgentLoop 调用。

    用法：
        cli = InteractiveCLI(agent_loop=loop, model="gpt-4o")
        cli.start()  # 进入交互循环
        # 或
        result = cli.run_once("列出当前目录下所有 Python 文件")
    """

    WELCOME_BANNER = """
whalefall v{version}
模型: {model} | Agent: {agent_type}
权限模式: {perm_mode}
输入 /help 查看命令。Ctrl+C 退出。
"""

    _CLI_SPECIFIC_HELP_LINES = (
        "/exit, /quit       退出",
        "/model <name>      切换模型（如 /model gpt-4o）",
        "/agent <type>      切换 Agent 类型（general/explore/plan/verify 或自定义）",
    )
    HELP_TEXT = (
        "\n可用命令：\n"
        + "\n".join(
            f"  {line}"
            for line in (*_CLI_SPECIFIC_HELP_LINES, *COMMON_HELP_LINES)
        )
        + "\n\n快捷键：\n  Ctrl+C             中断当前操作或退出\n"
    )

    def __init__(
        self,
        agent_loop=None,
        query_engine=None,
        model: Optional[str] = None,
        agent_type: str = "general",
        max_turns: Optional[int] = None,
        stream: bool = True,
        bypass_permissions: bool = False,
        request_id: Optional[str] = None,
    ):
        """
        Args:
            agent_loop: AgentLoop 实例（已配置 llm_client/registry/mcp）
            query_engine: QueryEngine 实例（可选，提供多轮会话管理）
            model: 使用的模型名
            agent_type: Agent 类型（general/explore/plan/verify）
            max_turns: 可选，覆盖 AgentConfig.max_turns
            stream: 是否流式输出
            bypass_permissions: 跳过权限检查
            request_id: 可选请求 ID（用于 trace）；交互模式下会自动附加回合号
        """
        self._loop = agent_loop
        self._query_engine = query_engine
        self._model = model
        self._agent_type = agent_type
        self._max_turns = int(max_turns) if max_turns is not None and int(max_turns) > 0 else None
        self._stream = stream
        self._bypass = bypass_permissions
        self._request_id = request_id
        # 默认每次 CLI 启动都使用新 session_id，避免跨重启继承旧上下文。
        # 若显式传入 request_id，则沿用 request_id 作为会话 id。
        self._session_id = (request_id or f"cli-{uuid4().hex[:12]}").strip() or f"cli-{uuid4().hex[:12]}"

        # 对话历史统计
        self._turns: int = 0
        self._total_tool_calls: int = 0

        # StreamHandler（用于回调）
        self._stream_handler: Optional[StreamHandler] = None

    # ------------------------------------------------------------------ #
    #                       主入口                                         #
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """进入交互循环。"""
        self._print_welcome()

        while True:
            try:
                user_input = self._read_input()
                if user_input is None:
                    break
                if not user_input.strip():
                    continue

                # 处理特殊命令
                if user_input.startswith("/"):
                    should_exit = self._handle_command(user_input.strip())
                    if should_exit:
                        break
                    continue

                # 普通查询
                self._run_query(user_input)

            except KeyboardInterrupt:
                print("\n（已中断）")
                try:
                    confirm = input("退出? [y/N] > ").strip().lower()
                    if confirm in ("y", "yes"):
                        break
                except (EOFError, KeyboardInterrupt):
                    break

        _print("\n再见！", style="bold green" if _HAS_RICH else "")

    def run_once(self, query: str) -> str:
        """
        非交互模式：执行单次查询并返回结果。

        Args:
            query: 用户查询

        Returns:
            Agent 最终回复文本
        """
        return self._run_query(query, print_result=True)

    # ------------------------------------------------------------------ #
    #                       查询执行                                       #
    # ------------------------------------------------------------------ #
    def _run_query(self, query: str, print_result: bool = False) -> str:
        """执行一次查询。"""
        from dataclasses import replace
        from whalefall.agent.roles import get_agent

        if self._query_engine is None and self._loop is None:
            _print("错误：QueryEngine/AgentLoop 未配置", style="red" if _HAS_RICH else "")
            return ""

        agent_config = get_agent(self._agent_type)
        if self._max_turns is not None:
            agent_config = replace(agent_config, max_turns=self._max_turns)

        # 创建 StreamHandler
        self._stream_handler = StreamHandler(
            on_print=self._stream_text,
            show_tools=True,
            show_compaction=True,
        )

        start_time = time.time()
        result = ""

        try:
            if _HAS_RICH:
                _console.print(Rule(f"[dim]Agent {self._agent_type}[/dim]"))

            if self._query_engine is not None:
                result = self._query_engine.submit(
                    session_id=self._session_id,
                    user_query=query,
                    agent_config=agent_config,
                    model=self._model,
                    request_id=self._build_request_id(),
                    on_text=self._stream_handler.on_text_delta if self._stream else None,
                    on_tool_start=self._stream_handler.on_tool_start,
                    on_tool_end=self._stream_handler.on_tool_end,
                    on_compaction=self._stream_handler.on_compaction,
                )
            else:
                # 兼容旧路径：无 QueryEngine 时退化为单轮执行
                result = self._loop.run(
                    user_query=query,
                    agent_config=agent_config,
                    model=self._model,
                    request_id=self._build_request_id(),
                    on_text=self._stream_handler.on_text_delta if self._stream else None,
                    on_tool_start=self._stream_handler.on_tool_start,
                    on_tool_end=self._stream_handler.on_tool_end,
                    on_compaction=self._stream_handler.on_compaction,
                )

            elapsed = time.time() - start_time
            stats = self._stream_handler.get_stats()
            self._total_tool_calls += stats["tool_calls"]
            self._turns += 1

            # 非流式交互模式：结果一次性打印；run_once 由调用方（main.py）负责打印
            if not self._stream and result and not print_result:
                _print(f"\n{result}")

            # 统计信息
            if _HAS_RICH:
                _console.print(
                    f"\n[dim]工具调用: {stats['tool_calls']} | "
                    f"耗时: {elapsed:.1f}s | "
                    f"输出: {stats['text_chars']} 字符[/dim]"
                )
            else:
                print(
                    f"\n[统计] 工具调用: {stats['tool_calls']} | "
                    f"耗时: {elapsed:.1f}s"
                )

        except KeyboardInterrupt:
            _print("\n（查询已中断）", style="yellow" if _HAS_RICH else "")
        except Exception as e:
            _print(f"\n错误: {e}", style="red" if _HAS_RICH else "")
            import traceback
            traceback.print_exc()

        return result

    def _build_request_id(self) -> Optional[str]:
        """构建本轮 request_id。"""
        if not self._request_id:
            return None
        # 交互模式多轮时避免覆盖同一 trace 文件
        return f"{self._request_id}-turn-{self._turns + 1}"

    def _stream_text(self, delta: str) -> None:
        """流式文本输出（每个 delta 调用）。"""
        print(delta, end="", flush=True)

    # ------------------------------------------------------------------ #
    #                       特殊命令处理                                   #
    # ------------------------------------------------------------------ #
    def _handle_command(self, cmd: str) -> bool:
        """
        处理特殊命令。

        Returns:
            True=应退出, False=继续
        """
        command, arg = parse_slash(cmd)

        if command in ("/exit", "/quit"):
            return True

        if command == "/help":
            _print(self.HELP_TEXT, style="cyan" if _HAS_RICH else "")
            return False

        if command == "/model":
            if arg:
                self._model = arg
                _print(f"模型已切换: {self._model}", style="green" if _HAS_RICH else "")
            else:
                _print(f"当前模型: {self._model or '默认'}")
            return False

        if command == "/agent":
            from whalefall.agent.roles import list_agent_names
            names = list_agent_names()
            if arg and arg in names:
                self._agent_type = arg
                _print(f"Agent 类型已切换: {self._agent_type}", style="green" if _HAS_RICH else "")
            else:
                _print(f"有效的 Agent 类型: {', '.join(names)}")
                _print(f"当前: {self._agent_type}")
            return False

        ctx = SlashContext(
            query_engine=self._query_engine,
            session_id=self._session_id,
            extra_stats_fn=lambda: {
                "对话轮数": self._turns,
                "总工具调用": self._total_tool_calls,
                "模型": self._model or "默认",
                "Agent 类型": self._agent_type,
            },
        )
        result = dispatch_common(cmd, ctx)
        if result.handled:
            if result.cleared and command == "/clear":
                import os
                os.system("clear" if os.name != "nt" else "cls")
                self._print_welcome()
            if result.message:
                style = "yellow" if _HAS_RICH else ""
                _print(result.message, style=style)
            return False

        _print(f"未知命令: {cmd}。输入 /help 查看帮助。", style="red" if _HAS_RICH else "")
        return False

    # /resume /init /clear /compact /stats 的业务逻辑已下沉到 ui.slash.core。

    # ------------------------------------------------------------------ #
    #                       输入/输出工具方法                               #
    # ------------------------------------------------------------------ #
    def _read_input(self) -> Optional[str]:
        """读取用户输入，返回 None 表示 EOF。"""
        try:
            prompt = f"\n[{self._agent_type}] > "
            if _HAS_RICH:
                return _console.input(f"[bold green]{prompt}[/bold green]")
            else:
                return input(prompt)
        except EOFError:
            return None

    def _print_welcome(self) -> None:
        """打印欢迎信息。"""
        banner = self.WELCOME_BANNER.format(
            version=VERSION,
            model=self._model or "默认",
            agent_type=self._agent_type,
            perm_mode=("bypass" if self._bypass else "ask"),
        ).strip()

        if _HAS_RICH:
            _console.print(Panel(banner, title="[bold blue]whalefall[/bold blue]", border_style="blue"))
        else:
            print("=" * 60)
            print(banner)
            print("=" * 60)

# ------------------------------------------------------------------ #
#                       CLI 工厂方法                                   #
# ------------------------------------------------------------------ #
def create_cli(
    model: Optional[str] = None,
    agent_type: str = "general",
    stream: bool = True,
    bypass_permissions: bool = False,
    enable_builtin_tools: bool = True,
    mcp_config_path: Optional[str] = None,
) -> "InteractiveCLI":
    """
    工厂方法：创建完整配置的 InteractiveCLI。

    自动完成：
    - LLMClient 初始化
    - ToolRegistry（内建工具）
    - MCPClient（如有配置）
    - PermissionManager
    - AgentLoop + QueryEngine（会话层）

    Args:
        model: LLM 模型名
        agent_type: Agent 类型
        stream: 是否流式输出
        bypass_permissions: 跳过权限检查
        enable_builtin_tools: 是否启用内建工具
        mcp_config_path: MCP 配置文件路径（None 用默认）

    Returns:
        已配置的 InteractiveCLI 实例
    """
    from whalefall.llm.llm_client import LLMClient
    from whalefall.agent.loop import AgentLoop
    from whalefall.agent.query_engine import QueryEngine
    from whalefall.permissions.manager import PermissionManager

    llm = LLMClient(model=model or "gpt-4o-mini")

    # 内建工具注册表
    registry = None
    if enable_builtin_tools:
        from whalefall.tools.registry import build_default_registry
        registry = build_default_registry()

    # MCP 客户端（工具 schema 由 AgentLoop._get_tools() 通过 mcp_client.list_tools() 获取）
    mcp_client = None
    try:
        from whalefall.mcp import MCPClient
        mcp_client = MCPClient(config_path=mcp_config_path)
        mcp_client.connect()
    except Exception as e:
        if _HAS_RICH:
            _console.print(f"[yellow]MCP 连接跳过: {e}[/yellow]")
        else:
            print(f"[警告] MCP 连接跳过: {e}")

    # 权限管理器
    perm_manager = (
        PermissionManager.create_bypass()
        if bypass_permissions
        else PermissionManager(interactive=True)
    )

    # AgentLoop
    loop = AgentLoop(
        llm_client=llm,
        tool_registry=registry,
        mcp_client=mcp_client,
        permission_manager=perm_manager,
    )
    query_engine = QueryEngine(loop)

    return InteractiveCLI(
        agent_loop=loop,
        query_engine=query_engine,
        model=model,
        agent_type=agent_type,
        stream=stream,
        bypass_permissions=bypass_permissions,
    )
