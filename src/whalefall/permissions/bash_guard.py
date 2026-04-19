"""
BashGuard：bash 命令静态安全分析。

基于模式匹配检测危险/警告级别的命令，不调 LLM，毫秒级返回。
设计参考 Claude Code 的 bashClassifier 思路，针对本地通用 agent 场景裁剪。

风险等级：
  SAFE   — 无已知风险，直接放行
  WARN   — 需要注意（如 sudo、force-push），仍允许用户决定
  DANGER — 危险命令，直接拒绝（如删根目录、格盘、fork bomb）
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple


class BashRisk(str, Enum):
    SAFE   = "safe"
    WARN   = "warn"
    DANGER = "danger"


@dataclass
class BashGuardResult:
    risk:   BashRisk
    reason: str  # 人可读说明，供权限提示展示


# ── 危险模式 (DANGER → auto-deny) ──────────────────────────────────────────

# (pattern, reason)
_DANGER_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # 删根/家目录
    (re.compile(r"\brm\s.*-[rfRF]{1,2}.*[\s/]*/\s*$"),    "危险：尝试删除根目录 /"),
    (re.compile(r"\brm\s.*-[rfRF]{1,2}.*~/?\s*$"),          "危险：尝试删除家目录 ~"),
    (re.compile(r"\brm\s.*-[rfRF]{1,2}\s+/\b"),             "危险：目标路径为 / 的递归删除"),
    # 磁盘操作
    (re.compile(r"\bdd\s.*of=/dev/(sd|hd|nvme|vd|xvd)"),   "危险：直接写入磁盘设备"),
    (re.compile(r"\bmkfs\b"),                                "危险：格式化磁盘（mkfs）"),
    (re.compile(r"\bfdisk\s"),                               "危险：磁盘分区工具（fdisk）"),
    (re.compile(r"\bparted\s"),                              "危险：磁盘分区工具（parted）"),
    # 系统破坏
    (re.compile(r":\(\)\s*\{"),                              "危险：Fork Bomb 特征"),
    (re.compile(r"\bshred\s.*-[znuz]*\s*/dev/"),             "危险：shred 擦除磁盘设备"),
    # 管道执行远程脚本
    (re.compile(r"\b(curl|wget)\b.*\|\s*(ba)?sh\b"),         "危险：远程脚本管道到 sh/bash 执行"),
    (re.compile(r"\b(curl|wget)\b.*\|\s*python\b"),          "危险：远程内容管道到 python 执行"),
    # 重定向到关键系统文件
    (re.compile(r">\s*/etc/(passwd|shadow|sudoers|crontab|hosts)"), "危险：覆盖关键系统文件"),
    (re.compile(r">\s*/dev/(sd|hd|nvme|vd)"),                "危险：重定向覆盖磁盘设备"),
    # 关机/重启
    (re.compile(r"\b(shutdown|halt|reboot|poweroff|init\s+0)\b"), "危险：系统关机/重启命令"),
    # iptables 清空
    (re.compile(r"\biptables\s+-F\b"),                       "危险：清空 iptables 防火墙规则"),
    (re.compile(r"\bip6?tables\s+--flush\b"),                "危险：清空防火墙规则"),
    # crontab 删除
    (re.compile(r"\bcrontab\s+-r\b"),                        "危险：删除所有 cron 任务"),
]

# ── 警告模式 (WARN → 提示用户，走正常 ASK 流程) ────────────────────────────

_WARN_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bsudo\b"),                                "注意：包含 sudo 权限提升"),
    (re.compile(r"\bgit\s+push\s+.*--force\b"),              "注意：强制 push（git push --force）"),
    (re.compile(r"\bgit\s+push\s+.*-f\b"),                   "注意：强制 push（git push -f）"),
    (re.compile(r"\bpip\s+install\s+.*--force-reinstall\b"), "注意：强制重装 pip 包"),
    (re.compile(r"\bnpm\s+install\s+.*--force\b"),           "注意：强制安装 npm 包"),
    (re.compile(r"\bchmod\s+777\b"),                         "注意：设置 chmod 777（全局可写）"),
    (re.compile(r"\bchown\s+.*root\b"),                      "注意：更改所有者为 root"),
    (re.compile(r"\bkill\s+-9\b"),                           "注意：SIGKILL 强制杀进程"),
    (re.compile(r"\bkillall\b"),                             "注意：killall 批量杀进程"),
    (re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", re.IGNORECASE), "注意：SQL DROP 语句"),
    (re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE),       "注意：SQL DELETE 语句"),
    (re.compile(r"\bTRUNCATE\b", re.IGNORECASE),             "注意：SQL TRUNCATE 语句"),
    (re.compile(r"\bsystemctl\s+(stop|disable|mask)\b"),     "注意：停止/禁用系统服务"),
    (re.compile(r"\bapt(-get)?\s+(remove|purge)\b"),         "注意：卸载系统软件包"),
    (re.compile(r"\byum\s+(remove|erase)\b"),                "注意：卸载系统软件包"),
]


def _shlex_tokens(cmd: str) -> List[str]:
    """安全地尝试 shlex 切词；解析失败返回空列表（由调用方 fallback 到原始字符串匹配）。"""
    try:
        return shlex.split(cmd, posix=True)
    except Exception:
        return []


def _basename(arg: str) -> str:
    """取路径的 basename，用于和 rm/shutdown 等命令名比较，避开 /usr/bin/rm 绕过。"""
    arg = (arg or "").strip()
    if not arg:
        return ""
    if "/" in arg or "\\" in arg:
        try:
            return os.path.basename(arg)
        except Exception:
            return arg
    return arg


_DANGEROUS_RM_TARGETS: frozenset[str] = frozenset({"/", "/*", "/.", "~", "~/", "/root", "/home"})


def _rm_targets_danger(tokens: List[str]) -> Optional[str]:
    """
    shlex 分段后，检查 rm 的目标是否为根/家目录。
    - 支持 /usr/bin/rm 这种绝对路径写法（取 basename）
    - 支持 --no-preserve-root 等额外标志
    """
    if not tokens:
        return None
    head = _basename(tokens[0])
    if head != "rm":
        return None
    has_recursive = any(
        t.startswith("-") and any(c in t for c in ("r", "R", "f", "F"))
        for t in tokens[1:]
    )
    if not has_recursive:
        return None
    for t in tokens[1:]:
        if t.startswith("-"):
            continue
        resolved = os.path.expanduser(t).rstrip()
        if resolved in _DANGEROUS_RM_TARGETS:
            return f"危险：rm -rf 目标为 {resolved!r}"
        try:
            p = Path(resolved).resolve(strict=False)
            if str(p) == "/" or str(p) == str(Path.home()):
                return f"危险：rm -rf 指向 {p}"
        except Exception:
            continue
    return None


def classify_command(cmd: str) -> BashGuardResult:
    """
    分析 bash 命令的安全风险。

    Args:
        cmd: 原始 bash 命令字符串

    Returns:
        BashGuardResult(risk=SAFE/WARN/DANGER, reason=...)
    """
    if not cmd or not cmd.strip():
        return BashGuardResult(risk=BashRisk.SAFE, reason="空命令")

    # shlex 分段检查（对 rm 等命令做路径归一化，避免 /usr/bin/rm、quoted 等绕过）
    for segment in re.split(r"[;&|]+|\$\(|`", cmd):
        tokens = _shlex_tokens(segment)
        danger = _rm_targets_danger(tokens)
        if danger:
            return BashGuardResult(risk=BashRisk.DANGER, reason=danger)

    # 正则模式（字符串层）作为补充覆盖
    for pattern, reason in _DANGER_PATTERNS:
        if pattern.search(cmd):
            return BashGuardResult(risk=BashRisk.DANGER, reason=reason)

    # 再检查警告模式（收集所有匹配，合并原因）
    warn_reasons: List[str] = []
    for pattern, reason in _WARN_PATTERNS:
        if pattern.search(cmd):
            warn_reasons.append(reason)

    if warn_reasons:
        return BashGuardResult(
            risk=BashRisk.WARN,
            reason="；".join(warn_reasons),
        )

    return BashGuardResult(risk=BashRisk.SAFE, reason="")


def is_dangerous(cmd: str) -> bool:
    """快捷判断：命令是否为危险级别。"""
    return classify_command(cmd).risk == BashRisk.DANGER


def is_safe(cmd: str) -> bool:
    """快捷判断：命令是否为安全级别。"""
    return classify_command(cmd).risk == BashRisk.SAFE


# ── 路径安全 ────────────────────────────────────────────────────────────────

# 绝对禁止写入的路径前缀（无论何种模式）
_PROTECTED_PATH_PREFIXES: List[str] = [
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
    "/etc/crontab",
    "/etc/hosts",
    "/boot/",
    "/dev/",
    "/proc/",
    "/sys/",
]


def is_protected_path(path: str) -> Tuple[bool, Optional[str]]:
    """
    检查路径是否为受保护的系统路径。

    使用 `Path.expanduser().resolve()` 做归一化，避免 `/etc/./passwd`、
    `/etc/../etc/passwd`、`~/../../etc/hosts` 之类绕过。

    Returns:
        (is_protected: bool, reason: Optional[str])
    """
    raw = (path or "").strip()
    if not raw:
        return False, None
    candidates = [raw]
    try:
        # os.path.normpath 处理掉 "./" / ".." 但不会 follow symlink（跨平台）
        candidates.append(os.path.normpath(os.path.expanduser(raw)))
    except Exception:
        pass
    try:
        # resolve() 会 follow symlink（在 macOS 上 /etc → /private/etc）
        resolved = Path(raw).expanduser().resolve(strict=False)
        candidates.append(str(resolved))
    except Exception:
        pass
    for candidate in candidates:
        for prefix in _PROTECTED_PATH_PREFIXES:
            # 同时允许 symlink 目标（macOS 上 /private/etc/passwd 也应命中 /etc/passwd）
            stripped = prefix.rstrip("/")
            if candidate.startswith(prefix) or candidate == stripped:
                return True, f"受保护的系统路径：{prefix}"
            # macOS: /private + /etc/... 是同一条目
            if candidate.startswith("/private" + prefix) or candidate == "/private" + stripped:
                return True, f"受保护的系统路径：{prefix}"
    return False, None
