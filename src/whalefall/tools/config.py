"""
ConfigTool：查询运行时配置信息。

支持操作：
- get：获取单个配置项
- list：列出所有可用模型
- info：显示当前运行时概览（模型、context window、工具数量等）
"""
from __future__ import annotations

from typing import Any, Dict

from whalefall.tools.base import BuiltinTool, ToolContext


_GETTABLE_KEYS = {
    "model":          "当前使用的模型别名",
    "context_window": "模型 context window（token 数）",
    "runtime_dir":    "运行态目录路径",
    "cwd":            "当前工作目录",
}


class ConfigTool(BuiltinTool):
    name = "config"
    description = (
        "查询当前运行时配置信息。\n"
        "action 可选：\n"
        "  - get：获取单个配置项（key 必填）\n"
        "    可用 key：model / context_window / runtime_dir / cwd\n"
        "  - list：列出所有可用模型\n"
        "  - info：显示运行时概览（模型、工具数量、目录等）"
    )
    read_only = True
    max_result_chars = 4_000

    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "list", "info"],
                "description": "操作类型",
                "default": "info",
            },
            "key": {
                "type": "string",
                "description": "action=get 时必填，可选值：model / context_window / runtime_dir / cwd",
            },
        },
        "required": ["action"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        action = (args.get("action") or "info").strip().lower()

        if action == "get":
            return self._get(args, ctx)
        elif action == "list":
            return self._list_models()
        else:
            return self._info(ctx)

    def _get(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        key = (args.get("key") or "").strip().lower()
        if not key:
            return f"错误：action=get 时必须提供 key，可用值：{', '.join(_GETTABLE_KEYS)}"
        if key not in _GETTABLE_KEYS:
            return f"错误：不支持的 key={key!r}，可用值：{', '.join(_GETTABLE_KEYS)}"

        import os
        llm = ctx.llm_client
        if key == "model":
            return getattr(llm, "model", "未知")
        elif key == "context_window":
            from whalefall.llm.config import get_model_context
            model = getattr(llm, "model", "")
            return str(get_model_context(model))
        elif key == "runtime_dir":
            from whalefall.core.runtime import runtime_root
            return str(runtime_root())
        elif key == "cwd":
            return os.getcwd()
        return "未知"

    def _list_models(self) -> str:
        try:
            from whalefall.llm.config import get_config
            cfg = get_config()
            if not cfg.has_section("models"):
                return "未找到 [models] 配置段"
            keys = cfg.options("models")
            # 找出所有 alias（有 _model 后缀的 key 前缀即为别名）
            aliases = sorted({k.replace("_model", "") for k in keys if k.endswith("_model")})
            if not aliases:
                return "未配置任何模型别名"
            lines = ["可用模型别名："]
            for alias in aliases:
                try:
                    model_name = cfg.get("models", f"{alias}_model")
                    lines.append(f"  {alias:<20} → {model_name}")
                except Exception:
                    lines.append(f"  {alias}")
            return "\n".join(lines)
        except Exception as e:
            return f"读取模型配置失败: {e}"

    def _info(self, ctx: ToolContext) -> str:
        import os
        from whalefall.core.runtime import runtime_root
        from whalefall.llm.config import get_model_context

        llm = ctx.llm_client
        model = getattr(llm, "model", "未知")
        context_window = get_model_context(model)
        registry = ctx.tool_registry
        builtin_count = len(registry) if registry is not None else 0
        mcp_count = 0
        if ctx.mcp_client is not None:
            try:
                mcp_count = len(ctx.mcp_client.list_tools())
            except Exception:
                pass

        lines = [
            "=== whalefall 运行时配置 ===",
            f"模型:           {model}",
            f"Context Window: {context_window:,} tokens",
            f"内建工具数:     {builtin_count}",
            f"MCP 工具数:     {mcp_count}",
            f"工作目录:       {os.getcwd()}",
            f"运行态目录:     {runtime_root()}",
            f"MCP 已连接:     {'是' if ctx.mcp_client is not None else '否'}",
        ]
        return "\n".join(lines)
