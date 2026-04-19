---
name: general
description: 通用 Agent，具备全部工具与 MCP 权限，可调用子 Agent
max_turns: 100
allow_write_tools: true
allow_subagent: true
include: [base_identity, system_prompt, guardrails, tool_references, env_info]
---
