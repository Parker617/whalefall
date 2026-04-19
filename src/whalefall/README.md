# whalefall

可黑客改造的本地 Agent 主循环：
**LLM ↔ 工具调用 ↔ 结果 ↔ 下一轮**，一进程打通 16+ 内建工具、MCP 插件、子 Agent、
三层上下文压缩、8 步权限闸门、写前落盘的 SQLite 会话、流式 CLI 和 FastAPI Web UI。

- 一切运行态（日志 / trace / 产物 / 会话库 / transcripts）都写在 `src/whalefall/.runtime/`
  下，不污染家目录、不写 `/tmp`。
- 所有 Agent 定义、Skill 文档、MCP 插件都收敛在 `src/whalefall/` 里，整体打包/部署一次搞定。

---

## 1. 目录结构

```
src/whalefall/
├── main.py                          CLI 入口
│
├── agent/                           运行时核心
│   ├── loop.py                      AgentLoop 主循环（单次请求）
│   ├── query_engine.py              QueryEngine（会话层 + write-ahead）
│   ├── executor.py                  工具并发调度 + 死循环检测
│   ├── compaction.py                三层上下文压缩 + circuit breaker
│   ├── hooks.py                     8 种 hook 事件（线程安全）
│   ├── events.py                    TextDelta / ToolStart / ToolEnd / Compaction / Done
│   └── roles/
│       ├── config.py                AgentConfig + WRITE_TOOLS + DEFAULT_INCLUDE
│       ├── parts.py                 PromptPart 枚举 + 静态/动态积木 + wrap_system_reminder
│       ├── loader.py                扫 AGENT.md → AgentConfig + render_system_prompt[_split]
│       └── definitions/<name>/AGENT.md    general / explore / plan / verify / echo-tester
│
├── tools/                           内建工具（都继承 BuiltinTool）
│   ├── base.py                      BuiltinTool / ToolContext / ToolResult
│   ├── registry.py                  ToolRegistry + build_default_registry
│   ├── read / write / edit / notebook_edit / glob / grep / bash
│   ├── fetch / web_search / web_browser
│   ├── subagent                     派生子 Agent（同步 / 后台 job）
│   ├── todo                         task_create / update / get / list
│   ├── plan_mode                    enter_plan_mode / exit_plan_mode
│   ├── ask                          ask_user_question（CLI/Web 结构化多选）
│   ├── sleep / config
│
├── skills/                          Agent Skills 文档库（仿 Claude Code）
│   ├── general/weather/SKILL.md     SKILLS_CATALOG 自动扫 **/SKILL.md 并注入 system prompt
│   └── demo/nested/{alpha,beta}/    LLM 用 `read` 工具按路径加载 SKILL.md 正文
│
├── mcp/                             MCP 协议层（纯协议，不把 server 自述塞进 prompt）
│   ├── client.py                    MCPClient（stdio / sse / http，annotations → is_destructive）
│   ├── config.yaml.example          MCP 连接模板（复制为 config.yaml 后用）
│   ├── server/                      `python -m whalefall.mcp.server`（FastMCP）
│   └── plugins/hello.py             echo / add / time_now
│
├── permissions/
│   ├── manager.py                   PermissionManager 8 步管道
│   └── bash_guard.py                bash 静态分类（DANGER / WARN / SAFE）
│
├── storage/                         所有落盘入口
│   ├── session_store.py             SQLite write-ahead（每条消息一行）
│   ├── transcripts.py               全量对话 JSONL 归档（只进不出）
│   ├── last_session.py              ~/.whalefall/.../last_session.txt
│   ├── trace.py                     TraceWriter（每请求一个 JSONL）
│   └── retention.py                 容量治理（LRU + TTL + 上限）
│
├── llm/                             LLM 接入
│   ├── llm_client.py                LLMClient 门面
│   ├── config.py                    模型别名 / context window
│   ├── config/llm_config.ini        本机敏感配置（.gitignore）
│   ├── gateway/                     OpenAI client 复用 + 响应解包
│   └── postprocess/                 JSON 清洗 / 长文清洗 / token 截断
│
├── ui/
│   ├── cli.py                       Rich 交互式 REPL（流式 + 斜杠命令）
│   ├── web.py                       FastAPI + WebSocket
│   ├── streaming.py                 StreamHandler + CompactionRecord
│   ├── slash/core.py                斜杠命令共享实现（CLI/Web 共用）
│   └── static/index.html            Web 单页前端
│
├── core/
│   ├── log.py                       Timer / get_logger / request_id
│   └── runtime.py                   runtime_root / state_dir / traces_dir ...
│
├── tests/                           pytest 回归套件
└── .runtime/                        运行态（WHALEFALL_RUNTIME_DIR 可覆盖）
    ├── logs/     traces/      artifacts/
    ├── tool_results/        transcripts/
    └── state/sessions.sqlite3
```

---

## 2. 一次请求的完整生命周期

以"用户输入一句话 → 拿到最终回复"为例。CLI / Web / Python API / 后台 job 都走这同一条链。

```
用户输入
   │
 ┌─▼──────────────────────── UI 层（ui/cli.py · ui/web.py · ui/slash/core.py）
 │  normalize_slash_input + parse_slash → 斜杠命令由 dispatch_common 处理，不进 QueryEngine
 │  其余输入 → QueryEngine.submit(sid, user_query, agent_config, ...)
 │
 ┌─▼──────────────────────── QueryEngine.submit（agent/query_engine.py）
 │  1. _normalize_session_id           空/None → "default"
 │  2. _ensure_session_loaded          首次碰到该 sid：SQLite 读全部历史
 │                                      + filter_unresolved_tool_uses 丢孤儿 tool_calls
 │                                      + FIFO 截到 max_history_messages（默认 400）
 │  3. with session_lock               同 sid 串行，不同 sid 并行
 │  4. history = _sessions[sid]        本轮之前的完整历史
 │  5. loop.run_with_messages(
 │        extra_messages=history,      ← 只传历史，system 另装
 │        on_message_commit=_commit)   ← ★ write-ahead 回调
 │                                      │
 │  6. 每产一条消息都回调 _commit(msg):  │
 │        · 内存 _sessions[sid].append │
 │        · append_transcript(sid,msg) → .runtime/transcripts/<sid>.jsonl
 │        · SessionStore.append_message(sid,msg) → sessions.sqlite3 立即写
 │        · 超 max_history 就 replace_session 回写截断后版本
 │  7. Verify Gate（可选，默认 off）
 │  8. record_last_session(sid)        → ~/.whalefall/runtime/state/last_session.txt
 │  9. 每 N 次请求 RuntimeRetention.run() 清旧文件
 │
 ┌─▼──────────────────────── AgentLoop.run_stream（agent/loop.py）
 │
 │  进 while 前一次性装好 messages：
 │    idx 0:    system      = render_system_prompt(agent, registry, mcp_client, model)
 │    idx 1?:   system      = <system-reminder>父 Agent 上下文</...>    （仅子 agent）
 │    idx N:    历史 user/assistant/tool  （extra_messages 传进来）
 │    idx N+1:  user        = 本轮 user_query              ★ _commit → SQLite + transcripts
 │    idx N+2?: system      = <system-reminder>session_start hook 上下文</...>（hook 返回时）
 │    idx N+3?: system      = <system-reminder>未完成任务</...>          （todo_store 有未完成时）
 │
 │  while step < max_turns:
 │     step 1.  ContextManager.check_and_compact        （见第 5 节）
 │               压缩触发且 ctx.recently_read/todo_store 非空时
 │               _insert_after_last_system 插入 0~2 条 <system-reminder>
 │               （读过的 SKILL.md 也记在 recently_read 里，统一恢复）
 │     step 2.  hook before_llm                         可改 messages/tools/model
 │     step 3.  llm.stream_with_tools → yield TextDeltaEvent
 │     step 4.  hook after_llm                          可改 content/tool_calls
 │              if 无 tool_calls → break（_commit 最终 assistant，落盘）
 │     step 5.  doom_loop_check                         近 3 轮 tool_calls 指纹相同 → raise
 │     step 6.  PermissionManager 8 步                  （见第 8 节）
 │     step 7.  hook before_tool                        可改 args
 │     step 8.  ToolExecutor.execute_batch              只读并发 / 写串行 / pending group
 │                 · 内建工具 → BuiltinTool.execute
 │                 · MCP 工具 → mcp_client.call_tool
 │                 · 超长结果外置 .runtime/tool_results/<tool_call_id>.txt
 │     step 9.  hook after_tool                         可改 content
 │              ★ 本轮 assistant(tool_calls) 与每条 tool 消息立刻 _commit → SQLite + transcripts
 │     step 10. append 到本轮 messages，step++
 │
 │  收尾：
 │    · yield DoneEvent（最终文本、steps、本轮 session_messages）
 │    · 所有消息早已在 step 8/9 _commit 里落盘，UI 侧只是拼最终文本
```

**核心不变量**

| 不变量 | 由谁保证 |
| --- | --- |
| `role=system` 永不进 SQLite | `SessionStore._VALID_ROLES = {user, assistant, tool}` |
| 历史永不出现孤儿 tool_calls | 加载时 `filter_unresolved_tool_uses` |
| 静态 system 前缀字节稳定 | `render_system_prompt_split` 只把 `ENV_INFO` 放 dynamic 段 |
| 续接会话不重建历史 | `_ensure_session_loaded` 只在内存空时 load；submit 只尾部追加 |
| 崩溃/断电最多丢 LLM 还没吐完的那段 delta | 每条 user/assistant/tool 当场 `append_message` |

---

## 3. 系统提示词装配（最详细）

实现：`agent/roles/parts.py` + `agent/roles/loader.py::render_system_prompt[_split]`。

### 3.1 一次 submit 里 `messages[0]` 由 6 块积木拼成

`agent.include` 声明顺序，用 `"\n\n"` 粘起来。静态前缀字节稳定（prompt cache 友好），
动态 `ENV_INFO` 永远落在最末尾（`render_system_prompt_split` 强制隔离）。

```
━━ 静态前缀（prompt cache 友好）━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[A] BASE_IDENTITY            「你是专业的 AI 助手...核心行为准则：...」
                             parts.py::BASE_IDENTITY
                             AgentLoop.run_*(system_prompt="...") 整体替换
                             这一块（同时自动跳过 [F]，节点模式用）

[B] SYSTEM_PROMPT            definitions/<agent>/AGENT.md body（agent 专属指令）
                             general 的 body 是空的；explore/plan/verify 各自有

[C] GUARDRAILS               「[诚实与执行约束] / [行动风险分级] / [系统旁白]」
                             parts.py::BEHAVIOR_GUARDRAILS

[D] TONE_STYLE               「[输出风格与引用格式]」
                             parts.py::TONE_STYLE
                             path:line / 无 emoji / 工具前不加冒号 / GH-flavored MD

[E] TOOL_REFERENCES          「[工具使用指引]」+ 每个 BuiltinTool.prompt() 汇总
                             只包含当前 agent 可见的工具（allow_write_tools 过滤后）

[F] SKILLS_CATALOG           「[可用 Skills]」+ 每条 "name — description (path)"
                             扫 `src/whalefall/skills/**/SKILL.md` 动态生成
                             LLM 用 `read` 工具按路径加载 SKILL.md 正文
                             （仿 Claude Code "Agent Skills"，所有 agent 看到相同目录）

━━ 动态后缀（每次 submit 开头重算一次）━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[G] ENV_INFO                 <env>
                             Working directory: /Users/xxx/proj
                             Is directory a git repo: Yes
                             Platform: Darwin
                             OS Version: Darwin 24.1.0
                             Shell: zsh
                             Python: 3.10.14
                             Date: 2026-04-19 22:05
                             </env>
                             You are powered by the model `gpt-4o-mini`.
```

### 3.2 整个对话里会出现的所有 `role=system` 消息（共 6 类）

| # | 名字 | 位置 | 何时出现 | 代码位置 |
| --- | --- | --- | --- | --- |
| S1 | idx 0 主 system（上面 A-G 的拼接） | 进 while 前一次装一次 | 必然 | `loop.py::_build_system_prompt` |
| S2 | `<system-reminder>父 Agent 上下文</...>` | 进 while 前 | 仅子 agent 被父唤起 | `loop.py` |
| S3 | `<system-reminder>session_start hook 上下文</...>` | 进 while 前 | 注册了 hook 且返回 `additional_context` | `loop.py` |
| S4 | `<system-reminder>未完成任务</...>` | 进 while 前 | `todo_store.active_tasks()` 非空 | `loop.py` |
| S5 | `<system-reminder>压缩后文件上下文恢复</...>` | while 循环内 | 压缩触发 **且** `ctx.recently_read` 非空（读过的 SKILL.md 也在此列表里） | `loop.py` |
| S6 | `<system-reminder>压缩后任务列表恢复</...>` | while 循环内 | 压缩触发 **且** `store.active_tasks()` 非空 | `loop.py` |

- S2~S6 都统一用 `parts.py::wrap_system_reminder(body, title=...)` 包成
  `<system-reminder>...</system-reminder>`（对齐 Claude Code 约定）。
- `BEHAVIOR_GUARDRAILS` 里明确告诉 LLM：这些标签是框架注入的旁白，不是用户新指令，
  相关就利用、不相关就忽略，回复里不要复述原文。

### 3.3 一次 submit 内什么会重算

| 字段 | 一次 submit 内 | 下次 submit |
| --- | --- | --- |
| `<env>` 里的日期/cwd/git/shell | ❌（idx 0 装完就冻结） | ✅ 重算 |
| 工具使用指引 / Skills 目录索引 | ❌ | ✅（换 agent / 新加 SKILL.md 时） |
| `agent` body | ❌（`load_agents()` 进程启动时缓存） | 改 AGENT.md + 进程重启 |

> 想每轮更新 `<env>` 时间戳？注册 `before_llm` hook 自己改 `messages[0]`；框架默认不做。

### 3.4 什么不在 messages 里 —— 走 `tools=[...]`

OpenAI function calling 的 `tools=[...]` 参数和 `messages` 是**并列字段**。
下列清单在这里传达，而不是塞进 system prompt：

- 每个内建工具的 function schema（`BuiltinTool.to_openai_schema()`）
- 每个 MCP 工具的 function schema（名字前缀 `<server>__<tool>`，带 server 自己声明的 description）

Skill 不走这条通道——skill 索引放在 system prompt 的 `[F] SKILLS_CATALOG`，
LLM 用 `read` 工具按路径读正文（仿 Claude Code "Agent Skills"）。

过滤规则（`ToolRegistry.schemas(agent_config)` + `AgentLoop._get_tools`）：

| agent 字段 | 类型 | general 默认 | 子 agent 默认 | 语义 |
| --- | --- | --- | --- | --- |
| `allow_write_tools` | bool | `true` | `false` | `false` 时剔除写工具（bash/write/edit/notebook_edit/task_create/task_update） |
| `allow_subagent` | bool | `true` | 按 AGENT.md | `false` 时剔除 `agent` 工具 |
| `allowed_mcp_servers` | `None / [] / [names]` | `None` | `None` | `None`=全开；`[]`=全禁；`[a,b]`=白名单 |

### 3.5 快速替换通道

| 做法 | 效果 |
| --- | --- |
| `AgentLoop.run_*(system_prompt="...")` | 整体替换 [A]，**自动跳过 [G]**（节点模式） |
| 改 `parts.py::BASE_IDENTITY` | 影响全部 agent 的 [A] |
| 改 `definitions/<agent>/AGENT.md` body | 只影响那个 agent 的 [B] |
| agent 的 `include` 里删掉某块 | 那块不渲染 |
| 极简：`include=[BASE_IDENTITY]` | 只有 [A] |

> ⚠️ 命名重影：入参叫 `system_prompt=`，内部形参叫 `custom_base`，替换的是
> **[A] BASE_IDENTITY**——不是 [B] 那块同名积木。想改 [B]，只能改 `AGENT.md` body。

### 3.6 Agent 定义：单文件 `AGENT.md`

```markdown
---
name: factor-verify
description: 从数据源独立复核因子计算逻辑
model: gpt-4.1                                         # 可选，覆盖 CLI --model
max_turns: 60
allow_write_tools: false                               # 只读
allow_subagent: false                                  # 禁止再派生子 agent
allowed_mcp_servers: [quant]                           # 可选，MCP server 白名单
include: [base_identity, system_prompt, guardrails, tone_style, tool_references, skills_catalog, env_info]
---

[Factor Verify] 从数据源独立复核...
```

- 内建 agent（`general/explore/plan/verify/echo-tester`）和自定义 agent 同一套格式，
  `list_agent_names()` 动态扫描 `definitions/`，**新增 agent 不改代码立即可用**，
  CLI `--agent` 的合法值也动态提供。
- `include` 不写就用 `DEFAULT_INCLUDE`（就是上面 `A→B→C→D→E→F→G` 顺序）。
- 所有 agent 看到相同的 skill 目录索引（CC 精神：skill 是"谁来都能看"的文档库），
  不提供 per-agent skill 过滤；如果要做权限隔离，请用 `allow_write_tools` /
  `allowed_mcp_servers` 限制能**执行**哪些操作。

### 3.7 静态/动态切分（prompt cache 友好）

```python
static_prefix, dynamic_suffix = render_system_prompt_split(
    cfg, registry=..., mcp_client=..., model="gpt-4o-mini"
)
# static_prefix 字节稳定，可作为 Anthropic cache_control 断点的一段
# dynamic_suffix 只含 <env> 块 + model 身份一行
```

静态前缀字节稳定跨 turn 这一点有 `test_static_prefix_byte_stable_across_calls`
与 `test_split_merge_matches_render_system_prompt` 守护。

---

## 4. 工具体系

whalefall 对 LLM 只暴露**两类**工具，全都走 OpenAI function calling 协议：

1. **内建工具**（`tools/` 下继承 `BuiltinTool`）
2. **MCP 工具**（`mcp/client.py::MCPClient` 加载的外部 server）

**Skill 不是工具**——对齐 Claude Code 的 "Agent Skills"：skill 是放在
`src/whalefall/skills/**/SKILL.md` 的 Markdown 文档库；system prompt 里注入
"name — description (path)" 的索引（`[F] SKILLS_CATALOG`），LLM 判断相关时
用内建 `read` 工具按路径读正文，和读普通文件同一套机制（也同样被压缩恢复保护）。
这样 skill 通道非常纯粹，新加 SKILL.md 不改代码、不改 permission，下次 submit
自动生效。

### 4.1 内建工具

位于 `tools/`，每个都继承 `BuiltinTool`：

- `name` 唯一工具名；`read_only` 决定是否可并发 + 是否走 ASK 管道
- `parameters_schema` 返回 OpenAI function-calling schema
- `execute(args, ctx)` 实际执行；异常由 `ToolExecutor` 捕获包成 `is_error=True`
- `prompt()` 返回该工具在 system prompt 中的使用指引（被 [E] 自动汇总）
- `to_openai_schema(agent_config)` 返回 function schema；参数保留给少量
  agent-aware 工具未来扩展，默认忽略

| 工具 | R/W | 用途 |
| --- | --- | --- |
| `read` | R | 读文件（offset/limit 分段；记 `recently_read` 供压缩恢复）—— SKILL.md 也走它 |
| `write` | W | 整文件写，自动建父目录 |
| `edit` | W | `old_string` / `new_string` 精准替换，支持 `replace_all` |
| `notebook_edit` | W | Jupyter 结构化编辑（tmp+rename 原子写，失败回滚） |
| `glob` | R | glob 模式找文件，按 mtime 排序 |
| `grep` | R | ripgrep 包装，3 种输出模式（content / files_with_matches / count） |
| `bash` | W | 子进程 shell；经 BashGuard 预检 |
| `web_fetch` | R | URL → Markdown |
| `web_search` | R | SearXNG 优先，DDG 后备；设 `TAVILY_API_KEY` 启用 Tavily |
| `web_browser` | R | Playwright 浏览器（导航 / 截图到 `.runtime/artifacts/`） |
| `agent` | R | 派生子 Agent（同步 / `background=true` 后台 job） |
| `task_create/update/get/list` | W/W/R/R | TodoWrite 风格任务看板 |
| `enter_plan_mode / exit_plan_mode` | R | 只规划不执行模式开关 |
| `ask_user_question` | R | 结构化多选问答（CLI/Web 都有 UI） |
| `sleep` | R | 阻塞等待（测试 / 节流） |
| `config` | R | 列出模型别名与 llm_config 概要 |

写/读判定的真相源（优先级由高到低）：
`BuiltinTool.read_only` > `MCPClient.is_destructive(name)` > 静态 `WRITE_TOOLS` 保底集合。

### 4.2 Skills（仿 Claude Code "Agent Skills"）

目录结构：

```
src/whalefall/skills/
├── general/weather/SKILL.md           # skill name: general/weather
├── finance/stock/factor/SKILL.md      # skill name: finance/stock/factor
└── demo/nested/{alpha,beta}/SKILL.md
```

SKILL.md 约定（与 CC 一致的最小子集）：

```markdown
---
description: 一句话说明这个 skill 是做什么的（≤ 300 字自动截断；缺省则取正文第一段）
---

# Skill Title

详细步骤 / SOP / 注意事项 / 模板 ...
```

### 装配流程

1. **索引注入**：每次 submit 渲染 system prompt 时，`collect_skills_catalog()`
   递归扫 `src/whalefall/skills/**/SKILL.md`，把 `"- name — description (path)"`
   列表作为 `[F] SKILLS_CATALOG` 写进 `messages[0]` 的静态前缀里。
2. **按需加载**：LLM 判断某 skill 相关时，调用 `read(file_path="src/whalefall/skills/.../SKILL.md")`
   读正文，和打开任何普通文件完全同一套机制。
3. **压缩恢复**：`ReadTool` 会把读过的 SKILL.md 记入 `ctx.recently_read`；
   autocompact 触发时由 S5 `<system-reminder>` 一并恢复，不需要单独的 skill 通道。

### 我们相较 CC 的简化

- **只扫一源**：仅 `src/whalefall/skills/`；不合并 `.claude/skills/` 或 `~/.claude/skills/`，
  避免第三方目录越权写东西到你的进程上下文。
- **所有 agent 看同一目录**：不做 per-agent skill 过滤。skill 本身是文档，
  拦"能读什么"意义不大；要做隔离请用 `allow_write_tools` / `allowed_mcp_servers`
  限制能**执行**什么。
- **无 per-skill `allowed-tools` frontmatter**：skill 正文可以写"建议用 xx 工具"，
  但实际能用哪些工具由 `AgentConfig` 决定，不让 SKILL.md 反向改写 agent 权限。
- 索引总预算 `_SKILLS_CATALOG_BUDGET = 8000` 字符，防止 SKILL.md 爆炸式增长
  把 system prompt 撑爆；超出会在末尾提示"还有 N 个 skill 未展示"。

### 4.3 MCP 工具（纯协议，外部）

`mcp/client.py::MCPClient` 按 `mcp/config.yaml` 起 server（stdio / sse / http），
启动时 `list_tools` 一次性拉回全部工具描述，按 `<server>__<tool>` 前缀放进 `tools=[...]`。

**MCP 通道保持纯净**：whalefall 只消费 MCP 协议本身——工具 schema + 工具 docstring
（`function.description`）通过 `tools=[...]` 告诉 LLM；**不读取** `InitializeResult.instructions`，
也**不在 system prompt 里聚合** server-level 说明文字。想让 LLM 知道"怎么用某个 MCP
工具"，请把指引写进该工具本身的 description（或用一个工具去拉它）。

- `annotations.destructiveHint` 驱动 `is_destructive(name)`，进而影响 `ToolExecutor`
  的并发策略与 `PermissionManager` 的写工具判定。
- `call_tool` 失败自动重连目标 server 并重试一次；超长结果按
  `max_result_chars` 截断并落 `.runtime/tool_results/`。

**默认配置加载顺序**：

1. 显式 `MCPClient(config_path=...)`（必须存在）
2. 环境变量 `WHALEFALL_MCP_CONFIG`（必须存在）
3. 包内 `mcp/config.yaml`（存在则读）
4. **都没有** → 自动回退到内建 demo（拉起 `python -m whalefall.mcp.server`，
   提供 `echo` / `add` / `time_now`）。`pip install whalefall` 后无需复制模板即可跑通链路。

### 4.4 "能见度"通道一张图

```
LLM 每次 submit 能看到的工具信息，分布在两个字段里：

messages[0]  (system prompt，静态前缀+<env> 动态尾)
├── [E] TOOL_REFERENCES          ← 当前 agent 可见 BuiltinTool.prompt() 汇总（写工具过滤）
└── [F] SKILLS_CATALOG           ← skills/**/SKILL.md 的 "name — description (path)" 索引
                                   （所有 agent 同一份；不含正文，LLM 用 `read` 按需加载）

tools=[...]  (function schema 列表)
├── 每个 BuiltinTool.to_openai_schema()（allow_write_tools / allow_subagent 过滤）
└── 每个 MCP 工具的 schema（<server>__<tool>，allowed_mcp_servers 过滤）
```

这一拆分的目的：**所有"运行时才知道具体条目"的内容都往 `tools=[...]` 放**（MCP 工具随
server 连接状态变动），**所有"方法论/目录"内容放 system prompt 静态前缀**（每 agent
字节稳定，LLM 端 prompt cache 能命中）。Skill 目录是静态文件，所以放前缀；MCP 工具列表
可能随 server 热插拔变动，所以放 tools。

---

## 5. 上下文压缩（三层，全异步）

`agent/compaction.py::ContextManager`。每轮主循环 step 1 调一次 `check_and_compact`，
按阈值逐层触发：

| 层 | 触发 | 动作 | 是否调 LLM |
| --- | --- | --- | --- |
| `microcompact` | 始终尝试 | 近 3 轮完整保留；更早轮次里白名单重型工具（read/write/edit/bash/glob/grep/web_*/notebook_edit）的结果按 `MICRO_TOOL_MAX_CHARS=8000` 截断；携带 `_ts` 且超过 30 分钟的工具结果直接清空 | ❌ |
| `autocompact` | token 占比 ≥ **0.85** × context window | 用 LLM 生成 9 段 `<summary>`（意图 / 已完成 / 当前状态 / 涉及文件 ...）替换早期 non-system；保留最近 6 轮完整；system 段**全量保留** | ✅（异步） |
| `hard_limit` | token 占比 ≥ **0.95** | 强制头尾截断（兜底）；从尾往前保留若干 non-system | ❌ |

**Circuit breaker**：autocompact 连续失败 3 次就停 LLM 摘要，只做 micro + hard。
**并发安全**：压缩命中判定用**本次调用的局部回调**，不读共享实例标志（避免多会话互相覆盖）。
**磁盘永远是完整记录**：压缩只作用在**本轮喂给 LLM 的工作副本**；SQLite + transcripts 从不被压缩改写。

> autocompact 生成的摘要是 `role=user`（不是 system）；压缩从不产生新的 system 消息。

---

## 6. 子 Agent（`agent` 工具）

`tools/subagent.py::AgentTool`：

- 同进程内 spawn 一个新的 `AgentLoop`（复用父的 llm / registry / mcp / perm）
- `subagent_type` 的合法值是 `list_agent_names()` 动态提供（新增 AGENT.md 立即可用）
- 父 agent 把子 agent 的**最终消息**作为 `tool_result` 接回来
- `background=true`：扔线程池跑，立即返回 `job_id`；再次调用传 `job_id` 取结果
  - 单次等待默认 60s（上限 1800s），可用 `WHALEFALL_AGENT_BG_TIMEOUT` 覆盖
  - 最多保留 50 个 job（`_MAX_BG_JOBS`），`_prune_done_jobs` 只清已完成
- 子 Agent 完整对话保存到 `.runtime/transcripts/YYYYMMDD_HHMMSS_<agent>.json`
- `allow_subagent: false` 的 agent 禁止再嵌套；子 Agent 可配自己的
  `allow_write_tools` / `allowed_mcp_servers` 做沙箱隔离（skill 目录对所有
  agent 可见，不做 per-agent 过滤）
- 启动时触发 `subagent_start` hook，允许外部注入上下文
- finally 里调 `AgentLoop.cleanup()` 释放引用，长会话里连续派子 agent 不会泄漏

---

## 7. Hook 生命周期（8 种事件）

`agent/hooks.py::HookManager`（线程安全）。约定：hook 接受 `dict payload`，返回 `dict` 作为新
payload（返回 `None` 不修改）。多个 hook 按注册顺序串行执行；单个 hook 抛异常**不阻断主流程**，
只打 warning 日志。

| 事件 | 载荷 | 返回里能改 |
| --- | --- | --- |
| `session_start` | `{agent_config, model, query, request_id}` | `additional_context`（变 S3） |
| `before_llm` | `{messages, tools, model, request_id, agent_config, step}` | `messages` / `tools` / `model` |
| `after_llm` | `{content, tool_calls, latency_ms, step, ...}` | `content` / `tool_calls` |
| `before_tool` | `{tool_call, name, args, step, ...}` | `args` |
| `after_tool` | `{name, content, is_error, metadata, step, ...}` | `content` / `is_error` |
| `on_error` | `{stage, error, step, request_id, agent_config}` | ✗（只记日志） |
| `subagent_start` | `{agent_type, prompt, max_turns, allow_write}` | ✗ |
| `tool_use_failure` | `{name, content, step, ...}` | ✗（监控） |

**默认 hook**（`build_default_hook_manager()`，CLI/Web 都用这个）：

- `on_error`：简短警告 + 完整 traceback
- `after_tool`：`tool_metrics` 记工具名 / 结果长度 / 是否出错

---

## 8. 会话与持久化

### 8.1 QueryEngine（会话层）

见第 2 节流程图。要点：

- 默认 `enable_persistence=True`；想纯内存跑传 `QueryEngine(..., enable_persistence=False)`
  或 `session_store=None`。
- 同一 `session_id` 串行、不同 sid 并行；每个 sid 有独立 `threading.Lock`。
- Verify Gate（`WHALEFALL_VERIFY_GATE_MODE`，默认 `off`）
  - `block`：verify 子 agent 判 FAIL → 最终输出被阻断
  - `repair`：FAIL → 自动加一回合让主 agent 修复；新 assistant 用
    `_rewrite_last_assistant → replace_session` 覆盖末尾那条，保证内存/磁盘一致
  - **只对 `general` 生效**；子 agent（explore/plan/verify/echo-tester）跳过
- 跨请求每 `WHALEFALL_RETENTION_RUN_EVERY`（默认 50）次触发一次 `RuntimeRetention.run()`

### 8.2 SessionStore（SQLite write-ahead）

`storage/session_store.py`：

- 表：`sessions(session_id, updated_at, created_at)` + `session_messages(session_id, ordinal, role, content, tool_calls_json, tool_call_id, tool_name, ts)`——**每条消息一行**，按 `ordinal` 严格有序
- 主路径：`append_message` / `append_messages`（每条/每批立即落盘）；
  `replace_session`（`save_session` 是它的别名）用于压缩回写或 Verify Gate 重写
- `_VALID_ROLES = {user, assistant, tool}` —— system 永不落盘
- `load_session` 读出后自动跑 `filter_unresolved_tool_uses`：
  assistant 的所有 tool_calls 都无匹配 → 整条丢弃；孤儿 tool 也丢弃
- per-session `threading.Lock` + `BEGIN IMMEDIATE`，8 线程 × 20 条并发不丢消息
- 老库兼容：首次打开发现 `sessions.messages_json` 列，自动拆成每行一条迁移并 DROP 旧列
- 位置：`.runtime/state/sessions.sqlite3`

### 8.3 Transcripts（只进不出的归档）

`storage/transcripts.py`：

- `.runtime/transcripts/<safe_sid>.jsonl` 每条 user/assistant/tool 一行，**不被 FIFO 削减**
- 任何失败都吞掉，绝不影响主流程
- 审计 / 复盘就翻这里

### 8.4 Last session

`storage/last_session.py`：一个字符串文件 `~/.whalefall/runtime/state/last_session.txt`，
每次 submit 成功顺手更新。`--resume-last` / `/resume-last` 用它一键跳回上次会话。

### 8.5 写-前落盘流水线

```
[submit]                     [AgentLoop.run_stream]                 [QueryEngine]
   │                              │                                      │
   │─ user_query ──────▶  _commit(user_msg) ─────────────────▶ on_message_commit
   │                              │                                      │
   │                          LLM stream …                               │
   │                              │                                      │
   │                       assistant + tool_calls                        │
   │                       _commit(assistant_tool) ──────▶ append_message → SQLite + transcripts
   │                              │                                      │
   │                         exec tool_calls                             │
   │                       _commit(tool_result) ─────────▶ append_message → SQLite + transcripts
   │                              │                                      │
   │                         …repeat…                                    │
   │                              │                                      │
   │                         final assistant                             │
   │                       _commit(final_msg) ────────────▶ append_message → SQLite + transcripts
   │                              │                                      │
   │◀──── final text ─────────────┘                                      │
   │                                                  record_last_session │
```

---

## 9. 权限 8 步管道

`permissions/manager.py::PermissionManager.check(tool_name, args, *, force_write=False)`：

```
Step 1   bypass_all         → ALLOW                   （--dangerously-bypass）
Step 1.5 pause_all          → DENY                    （--pause 全阻塞）
Step 2   session 级缓存      → always_allow / always_denied
Step 3   DEFAULT_ALLOW_TOOLS → ALLOW
         {read, glob, grep, agent, web_search,
          task_create/update/get/list,
          enter_plan_mode, exit_plan_mode,
          sleep, ask_user_question, config}
Step 4   glob 精细规则       → ALLOW                   （fnmatch 匹配参数值）
Step 5   bash → BashGuard
           DANGER           → DENY                    （直接拒，不问）
           WARN             → ASK（附警告）
Step 6   写工具路径约束      → DENY                    （write / edit / notebook_edit 写进受保护路径）
Step 6.5 指纹化 denial        → 同 (tool, md5(args)) 连拒 3 次 → 自动 DENY
Step 7   DEFAULT_ASK_TOOLS  → ASK                     （写工具 + web_fetch + web_browser）
Step 8   其它                → ALLOW
```

- 默认集合**严格互斥**：`DEFAULT_ALLOW_TOOLS ∩ DEFAULT_ASK_TOOLS = ∅`，由 `test_permissions.py` 守护。
- `interactive=False` 时所有 ASK 直接变 DENY（Web UI 默认就是非交互；想全放行需 `WHALEFALL_WEB_BYPASS=1`）。
- **BashGuard**（`permissions/bash_guard.py`）：
  - `shlex` 分段，支持 `; && || | $(...) \``
  - `rm -rf` 命中 `/ /* ~ ~/ /root /home` 或 resolve 后为根/家 → DANGER
  - 正则扫 fork bomb / `curl | sh` / `dd of=/dev/…` / `mkfs` / `shutdown` / `iptables -F` / `crontab -r` 等
  - `is_protected_path(path)` 检查 `raw / normpath / resolve()` 三种候选；macOS 上 `/etc/passwd` 与 `/private/etc/passwd` 视为同一条

---

## 10. UI

### 10.1 CLI（`ui/cli.py`）

- Rich 终端 UI，流式输出；Rich 不可用时降级纯文本
- readline 历史落在 `.runtime/state/`
- 启动参数：`--session-id <sid>` / `--resume-last`；详见第 12 节
- 斜杠命令：
  - 共享实现：`/clear /compact /resume [id] /resume-last /sessions /init /stats /help`
  - CLI 专属：`/exit /model <alias> /agent <name>`

### 10.2 Web（`ui/web.py`）

- FastAPI + WebSocket，前端 `ui/static/index.html`
- 默认 `host=0.0.0.0 port=8000`
- 权限：默认**非交互**；`WHALEFALL_WEB_BYPASS=1` 全放行（仅本机调试用）
- 冷启动：`WHALEFALL_WEB_COLD_START=1` → 每次刷新新 session，`/resume` 被禁用；持久化也跟着关
- 背压：并发发送上限 `WHALEFALL_WS_MAX_PENDING_SENDS=128`
- 顶栏按钮：
  - 🔄 `POST /api/reload` 软重载：重建 LLM / MCP / QueryEngine，重读 `llm_config.ini` 与 `mcp/config.yaml`；进程不重启，WS 不断；**Python 代码改动不生效**
  - ♻️ `POST /api/restart` 硬重启：`os.execv(sys.executable, [sys.executable] + sys.argv)` 自替换，保留启动参数；WS 断 3~5s 自动重连
  - `GET /health` 返回 `{ok, reloading, mcp_tool_count, model, error}`

### 10.3 斜杠命令共享实现（`ui/slash/core.py`）

| API | 作用 |
| --- | --- |
| `normalize_slash_input(q)` | 全角 `／ → /`、去零宽字符、trim |
| `parse_slash(text)` | 返回 `(command, arg)`；非斜杠输入 `("", text)` |
| `SlashContext` | `{query_engine, session_id, strict_cold_start, extra_stats_fn, cwd}` |
| `SlashResult` | `{handled, message, cleared, should_exit}` |
| `dispatch_common(text, ctx)` | 分发公共斜杠命令；未命中返回 `handled=False` |
| `format_session_list` | `/resume` 无参时的会话列表格式化 |

---

## 11. LLM 层

- `llm/llm_client.py::LLMClient` 门面：`call_llm / call_llm_async / stream_with_tools /
  count_tokens / truncate_by_tokens / truncate_head_tail / _clean_json / clean_main_text`
- `llm/gateway/`
  - `clients.py`：`normalize_base_url`（去尾 `/`）+ `client_cache_key` + 同步/异步 client LRU 缓存
  - `response.py`：`ChatCompletion` 解包，识别 "HTTP 200 但网关返回 `success=false` / `status_code<0`" 的业务错误
- `llm/postprocess/`
  - `json_cleaner.py`：从 LLM 杂乱输出里挖 JSON（剥 code fence、补未转义引号、平衡括号）
  - `text_cleaner.py`：去高频页眉页脚、按关键字截断尾部声明
  - `tokens.py::TokenUtils`：`tiktoken cl100k_base` 封装（`count / truncate / truncate_head_tail`；
    默认 head 0.7、tail 其余，中间插 `[... 中间内容已截断 ...]`）
- `llm/config/llm_config.ini`：模型别名 → `*_model / *_url / *_key / *_context` 一组；
  CLI `--model` 吃这里的别名。默认别名 `gpt-4o-mini`（`main.py` / `ui/web.py`）。

---

## 12. 运行

### 12.1 CLI

```bash
cd src
python -m whalefall.main                                      # 交互模式
python -m whalefall.main "列出本目录下所有 python 文件并统计行数"   # 单次
python -m whalefall.main --agent explore "搜索 *.ipynb"
python -m whalefall.main --agent plan "重构因子回测流程"
python -m whalefall.main --agent verify "复核这份分析"
python -m whalefall.main --model gpt-4.1 --no-stream "…"
python -m whalefall.main --bypass "…"                         # 危险：跳过所有权限询问
python -m whalefall.main --no-mcp --no-builtin "…"
python -m whalefall.main --resume-last                        # 读 last_session.txt 续接
python -m whalefall.main --session-id <sid>                   # 指定会话 id
```

### 12.2 Web

```bash
python -m whalefall.main --web --host 0.0.0.0 --port 8000
```

浏览器 `localStorage` 自动记住并续接最后一次会话 id，侧栏可一键切换。

### 12.3 MCP Server 单跑

```bash
python -m whalefall.mcp.server                                # stdio，由 MCPClient 拉起即可
```

### 12.4 测试

```bash
cd src/whalefall
python -m pytest tests/ -q                                    # 全量回归
python tests/test_roles_e2e.py                                # 端到端：agent + prompt 自检
```

### 12.5 Python API（嵌入）

```python
from whalefall.agent.roles import (
    AgentConfig, PromptPart,
    load_agents, list_agent_names, get_agent,
    render_system_prompt, render_system_prompt_split,
    wrap_system_reminder, collect_skills_catalog,
    is_write_tool, WRITE_TOOLS,
)
from whalefall.tools.registry import build_default_registry

registry = build_default_registry()
cfg = get_agent("explore")                   # 找不到自动回退到 general

sp = render_system_prompt(cfg, registry=registry, mcp_client=None, model="gpt-4o-mini")
static_prefix, dynamic_suffix = render_system_prompt_split(
    cfg, registry=registry, mcp_client=None, model="gpt-4o-mini",
)

# 节点模式：整体替换身份 + 自动跳过 <env>
sp_custom = render_system_prompt(
    cfg, registry=registry, custom_base="# 项目规范\n全部使用简体中文回答",
)
```

更高层接入推荐走 `QueryEngine`，自带 write-ahead 落盘：

```python
from whalefall.agent.query_engine import QueryEngine
from whalefall.agent.loop import AgentLoop
from whalefall.llm.llm_client import LLMClient

loop = AgentLoop(llm_client=LLMClient(model="gpt-4o-mini"),
                 tool_registry=registry, mcp_client=None)
qe = QueryEngine(loop)
answer = qe.submit(
    session_id="demo",
    user_query="帮我读 README.md",
    agent_config=get_agent("general"),
)
```

进程被 `SIGKILL`，下次 `qe.submit(session_id="demo", ...)` 会自动拉回完整历史
（孤儿 tool_calls 会被过滤掉）。

---

## 13. 环境变量

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `WHALEFALL_RUNTIME_DIR` | 覆盖 `.runtime/` 根目录 | `src/whalefall/.runtime` |
| `WHALEFALL_LAST_SESSION_FILE` | 覆盖 `last_session.txt` 位置 | `~/.whalefall/runtime/state/last_session.txt` |
| `WHALEFALL_MCP_CONFIG` | 覆盖 MCP 配置路径 | `src/whalefall/mcp/config.yaml` |
| `WHALEFALL_AGENT_BG_TIMEOUT` | 子 agent 后台 job 单次等待上限（秒） | `60`（上限 `1800`） |
| `WHALEFALL_VERIFY_GATE_MODE` | `off / block / repair` | `off` |
| `WHALEFALL_RETENTION_RUN_EVERY` | QueryEngine 每 N 次请求触发清理 | `50` |
| `WHALEFALL_RETENTION_TRACES_MAX_FILES` | trace 最多保留文件数 | `1000` |
| `WHALEFALL_RETENTION_TRACES_MAX_BYTES` | trace 总大小上限 | 1 GB |
| `WHALEFALL_RETENTION_ARTIFACTS_MAX_BYTES` | artifact 总大小上限 | 1 GB |
| `WHALEFALL_RETENTION_TOOL_RESULTS_MAX_BYTES` | 外置 tool_results 总大小上限 | 512 MB |
| `WHALEFALL_RETENTION_TRANSCRIPTS_MAX_BYTES` | transcripts 总大小上限 | 256 MB |
| `WHALEFALL_RETENTION_LOGS_MAX_BYTES` | logs 总大小上限 | 256 MB |
| `WHALEFALL_WEB_BYPASS` | Web UI 全量放行 | `0` |
| `WHALEFALL_WEB_COLD_START` | Web UI 冷启动（禁 `/resume`、关持久化） | `0` |
| `WHALEFALL_WS_MAX_PENDING_SENDS` | WebSocket 背压队列 | `128` |
| `SEARXNG_URL` | `web_search` 优先使用的 SearXNG | `http://localhost:8080` |
| `TAVILY_API_KEY` | 启用 Tavily 搜索后端（可选） | 未设置 |
| `MCP_LOG_STDOUT` | MCP server 日志到 stdout | `0` |

---

## 14. 可选依赖

- **web_search**：SearXNG（推荐本机跑 `searxng-docker`，或设 `SEARXNG_URL`）；后备 DDG（`pip install ddgs`）
- **web_browser**：`pip install playwright && playwright install chromium`
- **tiktoken**：压缩与 `TokenUtils` 依赖
- **MCP**：FastMCP 已支持 stdio / sse / http，无额外依赖

---

## 15. 默认行为速查（哪些默认开、哪些默认关）

| 特性 | 默认值 | 如何打开/关闭 |
| --- | --- | --- |
| SQLite 持久化 | **开** | `QueryEngine(enable_persistence=False)` / `session_store=None` |
| Transcripts 归档 | **开**（一条都不丢） | 删 `storage/transcripts.py` 的调用；或让 `append_transcript` 返回 `False` |
| Verify Gate | **关** | `WHALEFALL_VERIFY_GATE_MODE=block|repair` |
| Web 权限交互 | **关**（非交互，ASK 被拒） | `WHALEFALL_WEB_BYPASS=1` 全放行 |
| Web 冷启动 | **关**（热启动，持久化开） | `WHALEFALL_WEB_COLD_START=1` |
| 默认 hook | `on_error`（简短 + 全堆栈）+ `after_tool`（tool_metrics） | `build_default_hook_manager()` |
| `enter_plan_mode` | **关** | LLM 调 `enter_plan_mode` 工具打开；打开后工具结果只是"计划描述"，不真执行 |
| MCP | 无 config 时自动回退到演示 server | `--no-mcp` 关；改 `mcp/config.yaml` 接自定义 |
| MCP server-level instructions 注入 system prompt | **关**（恒定，不可开） | 对齐"MCP 只走协议"；要让 LLM 知道用法请写进该 MCP 工具自己的 description |
| Skills 索引注入 system prompt | **开** | 从 `DEFAULT_INCLUDE` 里移除 `PromptPart.SKILLS_CATALOG` 即可关闭；或单个 agent `include:` 里不声明 |
| 内建工具 | **全开** | `--no-builtin` 全关；或 `build_default_registry(include_web_search=False, ...)` 粒度控制 |
| 子 Agent 写权限 | explore/plan/verify/echo-tester **均关**；general **开** | AGENT.md frontmatter `allow_write_tools: true/false` |
| 权限 bypass | **关** | `--bypass` / `PermissionManager.create_bypass()` |

---

## 16. 设计原则

1. **单一事实源**：写工具真伪来自 `BuiltinTool.read_only` / `MCPClient.is_destructive`（`WRITE_TOOLS` 仅启动保底）；agent 定义就一份 AGENT.md，内建无特权。
2. **事实与规则分离**：规则（BASE / GUARDRAILS / TONE_STYLE）静态常量；事实（`<env>` / 工具指引 / Skills 索引）动态渲染。
3. **CLI / Web / 后台 job 走同一个 `QueryEngine`**，session 与压缩逻辑不分叉。
4. **所有落盘入口收敛到 `storage/`**，运行态收敛到 `.runtime/`，不散布 `open()`。
5. **Permission 8 步、Hook 8 种、Compaction 3 层**：显式、可读、可单测，所有默认集合互斥由 pytest 守护。
6. **所有 agent / skill / MCP 插件都在 `src/whalefall/` 内**——不做 CC 那种项目级/用户级多源加载；框架永不读取 cwd 下的 `*.md`，只扫包内的 `agent/roles/definitions/**/AGENT.md` 与 `skills/**/SKILL.md`，整体打包一次搞定。
7. **不给"日常助手"做长期记忆偏好**：记忆迭代由 skill 体系（SOP 文档）承载，而不是隐式地学习用户习惯，确保每次输出都可审计。
