"""
PermissionManager：工具权限管理。

权限检查管道（对齐 CC 设计）：
  Step 1:   bypass_all → ALLOW（dangerously-bypass 模式）
  Step 1.5: pause_all → 全阻塞（--pause 模式，禁止所有工具执行）
  Step 2:   session 级始终允许/拒绝缓存
  Step 3:   明确白名单工具 → ALLOW
  Step 4:   Glob 精细化规则 → ALLOW
  Step 5:   BashGuard 静态分析（仅 bash 工具）
              DANGER → DENY（不询问，直接拒绝）
              WARN   → ASK（附带警告信息）
  Step 6:   路径约束（write/edit/notebook_edit）
              写入受保护系统路径 → DENY
  Step 7:   写工具/ask_tools → ASK
  Step 8:   未知工具 → ALLOW（开放策略）
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from whalefall.agent.roles import WRITE_TOOLS, is_write_tool
from whalefall.permissions.bash_guard import BashRisk, classify_command, is_protected_path


def _call_fingerprint(tool_name: str, args: Dict[str, Any]) -> str:
    """同工具 + 同参数视为同一次调用；args 变化不继承历史拒绝记录。"""
    try:
        payload = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        payload = str(args)
    return hashlib.md5(f"{tool_name}::{payload}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PermissionRule:
    """
    精细化权限规则。
    tool:    目标工具名（精确匹配）
    pattern: fnmatch glob 模式，匹配参数值
    arg_key: 要匹配的参数键名；None 则匹配第一个字符串型参数值
    """
    tool: str
    pattern: str
    arg_key: Optional[str] = None

    def matches(self, tool_name: str, args: Dict[str, Any]) -> bool:
        if self.tool != tool_name:
            return False
        if self.arg_key:
            val = str(args.get(self.arg_key, ""))
        else:
            val = next((str(v) for v in args.values() if isinstance(v, str)), "")
        return fnmatch.fnmatch(val, self.pattern)


class PermissionLevel(str, Enum):
    ALLOW = "allow"    # 直接允许（无需询问）
    ASK   = "ask"      # 询问用户
    DENY  = "deny"     # 拒绝


# 默认需要询问的工具（写工具 + 危险工具）
# 注意：下方 DEFAULT_ALLOW_TOOLS 里明确列出的只读工具会从 ASK 集合中剔除，
# 避免出现 “既允许又询问” 的配置冲突（Step 3 会先于 Step 7 命中）。
_DEFAULT_ALLOW_TOOLS: frozenset[str] = frozenset({
    "read", "glob", "grep", "agent",
    "web_search", "skill",
    # 增量式任务管理工具族
    "task_create", "task_update", "task_get", "task_list",
    # 规划模式切换是纯控制，读/写状态均不落盘
    "enter_plan_mode", "exit_plan_mode",
    # 小工具：sleep 只会阻塞；ask_user 是向用户提问，自身不会破坏
    "sleep", "ask_user_question", "config",
})

DEFAULT_ASK_TOOLS: frozenset[str] = (
    frozenset(WRITE_TOOLS) | frozenset({
        "web_fetch",       # 网络访问默认需要确认（可调整）
        "web_browser",     # 浏览器截图会写 .runtime/artifacts，视为副作用
    })
) - _DEFAULT_ALLOW_TOOLS

DEFAULT_ALLOW_TOOLS: frozenset[str] = _DEFAULT_ALLOW_TOOLS

# 写工具中用于路径约束检查的参数键映射
_PATH_ARG_KEYS: Dict[str, str] = {
    "write":         "file_path",
    "edit":          "file_path",
    "notebook_edit": "file_path",
}


class PermissionManager:
    """
    工具执行权限管理器。

    模式：
    - bypass_all=True：跳过所有检查，始终允许（--dangerously-bypass）
    - 默认：内建规则 + BashGuard 静态分析 + 用户交互询问
    """

    def __init__(
        self,
        bypass_all: bool = False,
        ask_tools: Optional[Set[str]] = None,
        allow_tools: Optional[Set[str]] = None,
        allow_rules: Optional[List[PermissionRule]] = None,
        interactive: bool = True,
        enforce_path_constraints: bool = True,
        pause_all: bool = False,
    ):
        """
        Args:
            bypass_all:               跳过所有权限检查（危险模式）
            ask_tools:                需要询问的工具名集合（默认 DEFAULT_ASK_TOOLS）
            allow_tools:              始终允许的工具名集合（默认 DEFAULT_ALLOW_TOOLS）
            allow_rules:              精细化 glob 规则，匹配则直接 ALLOW
            interactive:              是否支持交互式询问（False 时 ASK 直接拒绝）
            enforce_path_constraints: 是否对写工具做路径安全检查
            pause_all:                全阻塞模式（--pause），禁止所有工具执行
        """
        self.bypass_all = bypass_all
        self.pause_all = pause_all
        self._ask_tools: Set[str] = set(ask_tools) if ask_tools is not None else set(DEFAULT_ASK_TOOLS)
        self._allow_tools: Set[str] = set(allow_tools) if allow_tools is not None else set(DEFAULT_ALLOW_TOOLS)
        self._allow_rules: List[PermissionRule] = list(allow_rules) if allow_rules else []
        self._always_allowed: Set[str] = set()    # session 级别始终允许
        self._always_denied: Set[str] = set()     # session 级别始终拒绝
        self.interactive = interactive
        self.enforce_path_constraints = enforce_path_constraints
        # 最近一次检查的附加信息（警告文本），供 ask_user 展示
        self._last_warn_reason: str = ""
        # 拒绝追踪：同"工具+参数指纹"被拒绝 N 次后自动拒绝（避免同一请求无限重问）；
        # 不同参数不继承历史，防止拒绝一次 `rm -rf a/` 导致所有 bash 被自动拒。
        self._denial_counts: Dict[str, int] = {}
        self._auto_deny_threshold: int = 3

    def check(
        self,
        tool_name: str,
        args: Dict[str, Any],
        *,
        force_write: bool = False,
    ) -> PermissionLevel:
        """
        检查工具执行权限（5步管道）。

        Returns:
            PermissionLevel.ALLOW / ASK / DENY
        """
        self._last_warn_reason = ""

        # Step 1: bypass_all
        if self.bypass_all:
            return PermissionLevel.ALLOW

        # Step 1.5: pause_all → 全阻塞模式（--pause）
        if self.pause_all:
            return PermissionLevel.DENY

        # Step 2: session 级别缓存
        if tool_name in self._always_allowed:
            return PermissionLevel.ALLOW
        if tool_name in self._always_denied:
            return PermissionLevel.DENY

        # Step 3: 明确白名单
        if tool_name in self._allow_tools:
            return PermissionLevel.ALLOW

        # Step 4: Glob 精细化规则放行
        if self._allow_rules and any(r.matches(tool_name, args) for r in self._allow_rules):
            return PermissionLevel.ALLOW

        # Step 5: BashGuard（仅 bash 工具）
        if tool_name == "bash":
            cmd = str(args.get("command", "")).strip()
            if cmd:
                guard = classify_command(cmd)
                if guard.risk == BashRisk.DANGER:
                    # 危险命令直接拒绝，不询问用户
                    self._last_warn_reason = guard.reason
                    return PermissionLevel.DENY
                if guard.risk == BashRisk.WARN:
                    # 警告级别：附带原因进入 ASK 流程
                    self._last_warn_reason = guard.reason

        # Step 6: 路径约束（针对文件写工具）
        if self.enforce_path_constraints and tool_name in _PATH_ARG_KEYS:
            path = str(args.get(_PATH_ARG_KEYS[tool_name], "")).strip()
            if path:
                protected, reason = is_protected_path(path)
                if protected:
                    self._last_warn_reason = reason or "目标为受保护系统路径"
                    return PermissionLevel.DENY

        # Step 6.5: 拒绝追踪——同 (tool, args) 被用户拒绝 N 次后自动拒绝
        call_fp = _call_fingerprint(tool_name, args)
        if self._denial_counts.get(call_fp, 0) >= self._auto_deny_threshold:
            self._last_warn_reason = (
                f"工具 {tool_name} 以相同参数已被连续拒绝 "
                f"{self._auto_deny_threshold} 次，本次会话自动拒绝"
            )
            return PermissionLevel.DENY

        # Step 7: 写工具/ask_tools
        if tool_name in self._ask_tools or force_write or is_write_tool(tool_name):
            return PermissionLevel.ASK

        # Step 8: 未知工具默认允许
        return PermissionLevel.ALLOW

    def ask_user(self, tool_name: str, args: Dict[str, Any]) -> bool:
        """
        交互式询问用户是否允许执行工具。

        Returns:
            True=允许, False=拒绝
        """
        if not self.interactive:
            return False

        print(f"\n[权限请求] 工具: {tool_name}")
        if self._last_warn_reason:
            print(f"[警告] {self._last_warn_reason}")
        if args:
            try:
                args_preview = json.dumps(args, ensure_ascii=False, indent=2)
                if len(args_preview) > 500:
                    args_preview = args_preview[:500] + "\n  ..."
            except Exception:
                args_preview = str(args)[:500]
            print(f"参数:\n{args_preview}")

        while True:
            try:
                choice = input("允许执行? [y=是/n=否/a=本次会话始终允许/d=本次会话始终拒绝] > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n（已中断，默认拒绝）")
                return False

            call_fp = _call_fingerprint(tool_name, args)
            if choice in ("y", "yes", "是"):
                self._denial_counts.pop(call_fp, None)  # 允许后重置该具体调用的拒绝计数
                return True
            elif choice in ("n", "no", "否"):
                self._denial_counts[call_fp] = self._denial_counts.get(call_fp, 0) + 1
                return False
            elif choice in ("a", "always", "始终允许"):
                self.allow_always(tool_name)
                print(f"[已记录] 本次会话始终允许: {tool_name}")
                return True
            elif choice in ("d", "deny", "始终拒绝"):
                self._always_denied.add(tool_name)
                print(f"[已记录] 本次会话始终拒绝: {tool_name}")
                return False
            else:
                print("请输入 y/n/a/d")

    def deny_reason(self) -> str:
        """返回最近一次 DENY 的原因（供调用方展示给 LLM）。"""
        return self._last_warn_reason or "权限被拒绝"

    def allow_always(self, tool_name: str) -> None:
        """本次 session 始终允许该工具，不再询问。"""
        self._always_allowed.add(tool_name)
        self._always_denied.discard(tool_name)

    def deny_always(self, tool_name: str) -> None:
        """本次 session 始终拒绝该工具。"""
        self._always_denied.add(tool_name)
        self._always_allowed.discard(tool_name)

    def add_ask_tool(self, tool_name: str) -> None:
        self._ask_tools.add(tool_name)

    def add_allow_tool(self, tool_name: str) -> None:
        self._allow_tools.add(tool_name)

    def add_allow_rule(
        self,
        tool: str,
        pattern: str,
        arg_key: Optional[str] = None,
    ) -> None:
        """
        添加精细化 glob 放行规则。
        例：add_allow_rule("bash", "git *", arg_key="command")
        """
        self._allow_rules.append(PermissionRule(tool=tool, pattern=pattern, arg_key=arg_key))

    @classmethod
    def pause_mode(cls) -> "PermissionManager":
        """全阻塞权限管理器（--pause 模式，禁止所有工具执行）。"""
        return cls(pause_all=True, interactive=False)

    @classmethod
    def create_bypass(cls) -> "PermissionManager":
        """创建绕过所有权限检查的管理器（--dangerously-bypass 模式）。"""
        return cls(bypass_all=True)

    @classmethod
    def create_non_interactive(cls) -> "PermissionManager":
        """创建非交互模式（所有 ASK 工具直接拒绝）。"""
        return cls(interactive=False)

    def __repr__(self) -> str:
        return (
            f"<PermissionManager bypass={self.bypass_all} "
            f"pause={self.pause_all} "
            f"ask_tools={len(self._ask_tools)} "
            f"allow_rules={len(self._allow_rules)} "
            f"session_allowed={len(self._always_allowed)}>"
        )
