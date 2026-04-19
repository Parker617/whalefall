"""
PermissionManager 回归测试。

覆盖：
  - P0-11 DEFAULT_ASK/ALLOW 默认集合互斥：read/agent 等不会同时出现在 ASK
  - P0-11 web_browser / web_fetch 默认需要 ASK
  - P0-12 denial 指纹：同工具不同参数不会继承历史自动拒绝
  - BashGuard 接入：/usr/bin/rm -rf / 直接 DENY
  - 路径约束：write 到 /etc/passwd 直接 DENY
"""
from __future__ import annotations

import pytest

from whalefall.permissions.manager import (
    DEFAULT_ALLOW_TOOLS,
    DEFAULT_ASK_TOOLS,
    PermissionLevel,
    PermissionManager,
)


def test_default_sets_disjoint() -> None:
    assert DEFAULT_ALLOW_TOOLS.isdisjoint(DEFAULT_ASK_TOOLS)
    # read / agent 是经典只读工具，必须在 ALLOW
    assert "read" in DEFAULT_ALLOW_TOOLS
    assert "agent" in DEFAULT_ALLOW_TOOLS
    # write / edit / notebook_edit / bash 必须在 ASK
    for name in ("write", "edit", "notebook_edit", "bash"):
        assert name in DEFAULT_ASK_TOOLS, name
    # web_browser 作为有副作用的网络工具默认 ASK
    assert "web_browser" in DEFAULT_ASK_TOOLS


def test_read_always_allow() -> None:
    pm = PermissionManager()
    assert pm.check("read", {"file_path": "/tmp/x"}) == PermissionLevel.ALLOW


def test_write_goes_ask() -> None:
    pm = PermissionManager(interactive=False)
    lvl = pm.check("write", {"file_path": "/tmp/x", "content": "y"})
    assert lvl == PermissionLevel.ASK


def test_bash_danger_auto_deny() -> None:
    pm = PermissionManager()
    lvl = pm.check("bash", {"command": "/usr/bin/rm -rf /"})
    assert lvl == PermissionLevel.DENY


def test_write_protected_path_denied() -> None:
    pm = PermissionManager(enforce_path_constraints=True)
    lvl = pm.check("write", {"file_path": "/etc/passwd", "content": "x"})
    assert lvl == PermissionLevel.DENY


def test_denial_fingerprint_is_arg_specific() -> None:
    pm = PermissionManager(interactive=False)
    # 模拟 bash 同参数被连续拒绝 3 次 → 自动 DENY
    args_a = {"command": "echo A > /tmp/a.txt"}
    for _ in range(3):
        pm._denial_counts[_fp(pm, "bash", args_a)] = pm._denial_counts.get(_fp(pm, "bash", args_a), 0) + 1
    lvl_a = pm.check("bash", args_a)
    assert lvl_a == PermissionLevel.DENY, "同工具同参数 3 次后应自动拒绝"

    # 但同工具、不同参数应不受影响，走正常 ASK 流程
    args_b = {"command": "echo B > /tmp/b.txt"}
    lvl_b = pm.check("bash", args_b)
    assert lvl_b == PermissionLevel.ASK, (
        "不同参数不应继承历史拒绝（P0-12 指纹要求）"
    )


def test_allow_always_overrides_ask() -> None:
    pm = PermissionManager(interactive=False)
    pm.allow_always("write")
    assert pm.check("write", {"file_path": "/tmp/x", "content": "1"}) == PermissionLevel.ALLOW


def test_deny_always_overrides_allow() -> None:
    pm = PermissionManager()
    pm.deny_always("read")
    assert pm.check("read", {"file_path": "/tmp/x"}) == PermissionLevel.DENY


def test_pause_mode_blocks_everything() -> None:
    pm = PermissionManager.pause_mode()
    assert pm.check("read", {}) == PermissionLevel.DENY
    assert pm.check("bash", {"command": "ls"}) == PermissionLevel.DENY


def test_bypass_allows_everything() -> None:
    pm = PermissionManager.create_bypass()
    assert pm.check("bash", {"command": "/usr/bin/rm -rf /"}) == PermissionLevel.ALLOW


# ── helpers ──────────────────────────────────────────────────────────────

def _fp(pm: PermissionManager, name: str, args: dict) -> str:
    from whalefall.permissions.manager import _call_fingerprint
    return _call_fingerprint(name, args)
