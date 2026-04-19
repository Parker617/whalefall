"""权限管理模块。"""
from .bash_guard import (
    BashGuardResult,
    BashRisk,
    classify_command,
    is_dangerous,
    is_protected_path,
    is_safe,
)
from .manager import PermissionLevel, PermissionManager, PermissionRule

__all__ = [
    "BashGuardResult",
    "BashRisk",
    "PermissionLevel",
    "PermissionManager",
    "PermissionRule",
    "classify_command",
    "is_dangerous",
    "is_protected_path",
    "is_safe",
]
