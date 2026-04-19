# whalefall

通用型本地 Agent 框架：在一个进程内驱动 **"LLM → 工具调用 → 工具结果 → 下一轮"** 的主循环，
内建 16+ 个本地工具，通过 MCP 外挂任意领域插件（本仓自带 `hello` 演示插件），支持子 Agent、
三层上下文压缩、8 步权限闸门、持久化会话、流式 CLI 与 FastAPI Web UI。

**一切运行态（日志 / trace / 产物 / 会话库 / transcript）都落在 `src/whalefall/.runtime/` 下**，
不写用户主目录，不写 `/tmp`。所有 agent 定义、skill 文档、MCP 插件也全部收敛在 `src/whalefall/` 内，
方便整体打包、部署和隔离。

---

## 一、整体分层

```
┌────────────────────────────────────────────────────────────┐
│ UI 层                                                       │
│   ui/cli.py            Rich 交互式 REPL（流式输出 + 斜杠命令）│
│   ui/web.py            FastAPI + WebSocket（单页前端）       │
│   ui/slash/core.py     斜杠命令共享实现（CLI/Web 共用）      │
├────────────────────────────────────────────────────────────┤
│ QueryEngine（会话层，多轮上下文）                             │
│   session_id → 历史消息 → SQLite 持久化 → verify gate       │
├────────────────────────────────────────────────────────────┤
│ AgentLoop（执行层，单次请求）                                 │
│   while step < max_turns:                                  │
│       压缩 → LLM → 工具调用 → 权限 → 执行 → append           │
│   三类旁路组件：Hooks / PermissionManager / ToolExecutor      │
├────────────────────────────────────────────────────────────┤
│ LLM 层           llm/llm_client.py → gateway + postprocess  │
│ MCP 层           mcp/client.py + mcp/server（FastMCP 插件）  │
│ Storage 层       storage/ + .runtime/（SQLite + JSONL）     │
└────────────────────────────────────────────────────────────┘
```

> 想看每一层在一次请求里到底何时被调用、谁喂谁、谁拦谁，直接读下一节（**包含系统提示词完整装配步骤、续接 vs 新开、可替换参数、write-ahead 落盘**）；
> 各模块（压缩 / 子 agent / 工具 / hook / MCP …）的细节在第六章之后逐一展开。

---

## 二、一次请求的完整生命周期

以"用户输入一句话 → 拿到最终回复"为例，下面按控制流一段段拉直，**每一段都标出对应代码位置、哪些字段是"每次重算"、哪些是"整个进程寿命稳定"**。CLI、Web、Python API、后台 job 都走这同一条链。

### 2.0 入口分流：续接 or 新开

`session_id` 是谁说了算，直接决定这条链从哪条历史上接续：

| 入口 | 默认策略 | 续接方式 |
| --- | --- | --- |
| Web UI | 浏览器 `localStorage` 记 sid；新标签首次访问生成 UUID | 左栏列会话；点一条 → 传那个 sid |
| CLI 默认 | 每次启动生成新 sid | —— |
| `whalefall --resume-last` | 读 `~/.whalefall/runtime/state/last_session.txt` | 自动接上次没聊完的 |
| `whalefall --session-id <sid>` | 显式指定 | 直接用这个 sid |
| 交互里 `/resume <sid>` / `/resume-last` / `/sessions` | 运行时切换 | 无需重启进程 |
| Python API | 调用方传 `session_id` 字符串，建议外部稳定持久化 | —— |

**关键语义**：凡是 sid 在 SQLite 里已存在，就**只在尾部追加**，绝不重建历史；凡是 sid 是新的，`_sessions[sid]` 为空 list，本次 submit 的 user 成为历史第一条。

---

### 2.1 UI 层：把用户输入塞给 QueryEngine

**位置**：`ui/cli.py`、`ui/web.py`、`ui/slash/core.py`

- 输入先做 `normalize_slash_input`：全角 `／` → `/`、去零宽字符、trim
- 斜杠命令（`/clear /compact /resume /init /stats /help ...`）由 `ui/slash/core.py::dispatch_common` 直接处理，**不进 QueryEngine**
- 非斜杠输入 → `QueryEngine.submit(session_id=sid, user_query=..., agent_config=..., ...)`

---

### 2.2 QueryEngine：会话层（write-ahead）

**位置**：`agent/query_engine.py::submit`

```
submit(sid, user_query, agent_config, ...) ───────────────────────────────────
  1. _normalize_session_id          空串/None → "default"
  2. _ensure_session_loaded         首次碰到该 sid → load_session(sid)
                                    · SQLite 里读所有 session_messages
                                    · filter_unresolved_tool_uses 丢掉孤儿 tool_calls
                                    · FIFO 截到 max_history_messages（默认 400）
  3. with session_lock              同 sid 串行、不同 sid 并行
  4. history = _sessions[sid]       本轮之前的完整历史
  5. loop.run_with_messages(
         user_query=query,
         extra_messages=history,    ← 只传历史，system 另装
         agent_config=agent_cfg,
         on_message_commit=_commit, ← ★ write-ahead 回调
         ...)
  6. _commit(msg) 被 AgentLoop 每产一条消息回调一次：
         · self._sessions[sid].append(msg)
         · append_transcript(sid, msg)       → .runtime/transcripts/<sid>.jsonl
         · self._store.append_message(sid, msg) → sessions.sqlite3 立即写
         · 超 max_history_messages → 内存 FIFO 截老（SQLite 不截，transcripts 不截）
  7. Verify Gate（WHALEFALL_VERIFY_GATE_MODE=off/block/repair）
  8. record_last_session(sid)        → ~/.whalefall/runtime/state/last_session.txt
  9. 每 N 次请求 RuntimeRetention.run() 清旧文件
```

**"write-ahead" 的含义**：
user 消息、assistant 文本、每一次 tool 执行结果 —— **任何一条消息被生成的那一刻就写进 SQLite**。进程被 `SIGKILL` 最多丢解码缓冲里还没提交的一小段文本，下次 `load_session` 自动过滤掉没写完的 `tool_calls`（对齐 Claude Code 的 `filterUnresolvedToolUses`）。

---

### 2.3 `messages` 彻底解剖

> 看完这节你能回答：一次 submit 里 LLM 究竟看到了什么、什么时候看到、什么时候会变、什么时候不会变。

#### 2.3.0 最重要的一条规则：**一次 submit = 一锅端**

`AgentLoop.run_stream` 在进 `while step < max_turns` **之前**把 `messages` 一次性装好（下面列的 S1~S4 + 历史 + 本轮 user），然后**整个多轮循环都用同一个 `messages` 实例**，只做两件事：

- 尾部追加：每轮 LLM 吐的 `assistant` 和每个工具的 `tool` 结果
- 中间插入：压缩触发那一轮，把 S5/S6/S7 插到"最后一条 system 之后"

**换句话说**：idx 0 那条 system 在一次 submit 内只装配一次；之后整个多轮都共享同一份——env_info 里的时间戳、工具使用指引、agent body 在 submit 内**不会**因为跑了多轮而更新。

| 字段 | 一次 submit 内会变吗 | 什么时候才变 |
| --- | --- | --- |
| env_info 里的时间戳 / cwd | ❌ 整个 submit 共享开头那一刻 | **下一次 submit** 重渲时 |
| 工具使用指引 [D] | ❌ | 下一次 submit，且切了 agent / 改了权限 |
| agent body [B] | ❌（启动后 `load_agents()` 缓存住） | 改文件 + 进程重启 |
| skill 清单（在 `tools=[]` 里） | ❌ | 下一次 submit |
| MCP 工具描述（在 `tools=[]` 里） | ❌ | 下一次 submit |

**一次 submit 内真正每轮都会发生的只有 3 件事**：

1. 每轮开头检查压缩——命中阈值才真压缩；真压缩的那一轮会中间插 0~3 条 S5/S6/S7 便签
2. 每轮必然 append 1 条 assistant 到尾部
3. 有 tool_calls 时 append 0~N 条 tool 到尾部

（想每轮更新 env_info 时间戳？注册 `before_llm` hook 自己改 `messages[0]`；框架默认不做。）

---

#### 2.3.1 一次 submit 里 `messages` 长什么样（实物图）

##### A. 全新会话（最精简只有 2 条：1 条 system + 1 条 user）

```text
┌── 必/条件 ── idx ── role ────── 内容 ────────────────────────────────
│
│   必然       0     system    S1：render_system_prompt(agent) 的完整输出
│                              "你是专业的 AI 助手...[工具使用指引]...当前环境信息：..."
│
│  【条件】    1     system    S2：父 agent 上下文
│                              条件：仅子 agent 被父唤起时出现
│
│   必然       2     user      本轮 user_query
│                              （同时走 _commit 立刻写进 SQLite + transcripts）
│
│  【条件】    3     system    S3：session_start hook 返回的 additional_context
│                              条件：注册了 hook 且 hook 返回了该字段
│
│  【条件】    4     system    S4：pending_tasks_reminder
│                              "[未完成 TODO]\n- [ ] ..."
│                              条件：todo_store 里还挂着未完成任务
│
└── 以上全部在 while 之前一次装好；下面进入 while 循环 ─────────────────
```

##### B. 续写第 N 轮（唯一差别是"历史"那一段非空）

```text
┌── idx ── role ────── 内容 ────────────────────────────────
│
│    0     system    S1：render_system_prompt 本轮重渲（env_info 时间是这次 submit 开始那一刻）
│
│   【S2 父 agent 上下文，条件】
│
│  ──────── 历史（由 QueryEngine 从 SQLite 读出、传进 extra_messages）─────
│    2     user      N 轮前的 user_query
│    3     assistant N 轮前的 assistant + tool_calls
│    4     tool      N 轮前的 tool 结果
│    5     assistant N 轮前的最终回复
│    6     user      再上一轮 ...
│    ...             （可能几十条，最多 SQLite.max_history_messages 条）
│  ──────── 历史结束 ────────────────────────────────────────────────────
│
│    K     user      本轮新 user_query   ★ write-ahead 立刻落 SQLite
│
│   【S3 hook，条件】【S4 pending_tasks，条件】
│
└── 以上全部在 while 之前一次装好 ───────────────────────────────────────
```

> **续会话和新会话的唯一差别就是"历史那段有没有东西"**。S1/S2/S3/S4 装配逻辑、位置、规则完全一样；本轮 user 永远挂在历史末尾、S3/S4 之前。

---

#### 2.3.2 idx 0 那条 system 内部怎么拼（5 块积木）

`render_system_prompt(agent)` 按 `agent.include` 声明的顺序，把下列 5 块积木用 `"\n\n"` 粘起来。**唯一的硬规则**是：`ENV_INFO` 无论写在 include 第几位，都会被 `render_system_prompt_split()` 自动挪到**末尾**（保证前缀字节稳定、命中 prompt cache）。

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[A] BASE_IDENTITY
    "你是专业的 AI 助手，在一个交互式对话环境中通过工具调用解决用户问题。
     核心行为准则：..."
    ← parts.py::BASE_IDENTITY 常量
    ← AgentLoop.run_*(system_prompt="...") 入参会整体替换这一块
      （同时自动跳过 [E]；典型用途：量化节点场景）

[B] agent body（definitions/<agent>/AGENT.md 里 frontmatter 之后的正文）
    "[Explore Mode] 只读探索模式..."
    ← general 的 body 是空的；explore/plan/verify 各自有专属指令

[C] BEHAVIOR_GUARDRAILS
    "[诚实与执行约束]
     - 不要编造工具结果..."
    ← parts.py::BEHAVIOR_GUARDRAILS 常量

[D] 工具使用指引汇总
    "[工具使用指引]
     # read - 读取文件内容
     # bash - 执行命令 ...
     # skill - 当任务匹配某个 skill 时，先调用 `skill` 工具加载全文..."
    ← 每个 BuiltinTool.prompt() 串起来；只包含当前 agent 可见的工具
    ← 只有"工具怎么用"的常量文本；"当前可用 skill / MCP 工具有哪些"在
      tools=[...] 参数的 function.description 里传达（见 2.3.4）

━━━━━━━━━━ 以上前缀字节稳定（prompt cache 命中友好）━━━━━━━━━━━━

[E] ENV_INFO
    "当前环境信息：
     - 日期时间：2026-04-18 22:05
     - 工作目录：/Users/xxx/proj
     - 平台：Darwin 24.1.0 / Python 3.10
     - 本环境支持斜杠命令：/help /clear /stats ..."
    ← render_env_info() 在每次 submit 开头重算一次；注意：submit 内部
      跑多轮不会重新渲染（见 2.3.0）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**前 4 块 A/B/C/D 的相对顺序跟随 `include` 列表声明顺序**——不是代码硬编码。当前 4 个默认 agent 的 include 恰好让最终输出都是 `A→B→C→D→E`，但换个 agent 把 include 写成 `[guardrails, base_identity, tool_references, system_prompt, env_info]`，输出就变成 `C→A→D→B→E`（E 仍然钉末尾）。

**快速替换通道**：

| 做法 | 效果 |
| --- | --- |
| `AgentLoop.run_*(system_prompt="...")` | 整体替换 [A]，并**自动跳过 [E]**（节点模式） |
| 改 `parts.py::BASE_IDENTITY` | 影响全部 agent 的 [A] |
| 改 `definitions/<agent>/AGENT.md` body | 只影响那个 agent 的 [B] |
| agent 的 `include` 里删掉某块 | 那块不渲染（比如极简 `include=[BASE_IDENTITY]` 就只有 [A]） |

> ⚠️ **命名重影**：入参叫 `system_prompt=`，内部形参叫 `custom_base`，替换的是 **[A]**——不是 [B] 那块同名积木。想改 [B]，只能去改 `AGENT.md` body。

---

#### 2.3.3 一次对话里所有会出现的 `role=system` 消息（总共 7 种）

| # | 名字 | 在哪出现 | 触发条件 | 代码位置 |
| --- | --- | --- | --- | --- |
| **S1** | 主 system prompt（idx 0） | **进 while 前**（一次 submit 装一次） | 必然 | `loop.py` L198 |
| **S2** | `[父 Agent 上下文]` | 进 while 前 | 仅子 agent 被父唤起 | L207-L212 |
| **S3** | session_start hook 附加上下文 | 进 while 前 | 注册了 hook 且返回 `additional_context` | L242 |
| **S4** | `[未完成 TODO]` pending_tasks_reminder | 进 while 前 | `todo_store` 有未完成任务 | L266 |
| **S5** | `[最近读过的文件]` | **while 循环内**（压缩触发那一轮） | 压缩触发 **且** `ctx.recently_read` 非空 | L304 |
| **S6** | `[已加载的 skills]` | while 循环内 | 压缩触发 **且** `ctx.invoked_skills` 非空 | L309 |
| **S7** | `[待办列表]` | while 循环内 | 压缩触发 **且** `ctx.todo_list` 非空 | L314 |

**S1~S4 都在进 while 之前一次性装好**（每次 submit 一次）；**S5~S7 只在 while 循环里压缩触发的那一轮出现**（压缩触发多次就反复插），用 `_insert_after_last_system` 插到"最后一条 system 之后"而不是 append 到尾部。

**"可选" ≠ "每轮都可能触发"**。上表"触发条件"那列才是真实规则——比如 S3 就是"hook 返回了 additional_context"，跟 while 轮数没关系；S4 是"`todo_store` 有未完成任务"；S5~S7 则确实跟每轮的压缩检查绑定。

**三条容易误会的澄清**：

1. **autocompact 的摘要是 `role=user`，不是 system**（`compaction.py` L306）。压缩本身不产生新 system——它只是把早期 non-system 替换成**一条 user 摘要**，原有 system 全部保留。
2. **hard_truncate 也不产生新 system**——只是"保留全部 system + 从尾往前保留若干 non-system"直到够阈值。
3. **hook `before_llm` 理论上能在每轮重写 messages**，可以塞任何东西（包括多条 system），但那是用户自定义代码的行为，不算框架自动生成的 system。

---

#### 2.3.4 什么东西**不在 messages 里**——走 `tools=[...]` 参数

**skill 目录清单 / MCP 工具的 description / 所有 BuiltinTool 的 function schema**，走 OpenAI function calling 协议的 `tools=[...]` 参数（和 `messages` 是**并列的两个独立字段**）：

```python
tools = [
    {"type": "function", "function": {
        "name": "read",
        "description": "文件读取...",
        "parameters": {...}}},
    {"type": "function", "function": {
        "name": "bash",
        "description": "Shell 命令...",
        "parameters": {...}}},
    {"type": "function", "function": {
        "name": "skill",
        "description": "按名称加载本地技能文档...\n"
                       "可用技能：\n- weather: 查询天气\n- finance/stock/xxx: ...",
        "parameters": {...}}},
    {"type": "function", "function": {
        "name": "<mcp_server>_<tool>",
        "description": "(该 MCP 工具的描述)",
        "parameters": {...}}},
    ...
]
```

**过滤规则**（由 `ToolRegistry.schemas(agent_config)` 统一执行）：

| agent 字段 | 类型 | general 默认 | 子 agent 默认 | 含义 |
| --- | --- | --- | --- | --- |
| `allow_write_tools` | bool | `true` | `false` | false 时砍 bash/write/edit/notebook_edit/task_* |
| `allowed_mcp_servers` | `None / [] / [names]` | `None` | `None` | `None`=全开；`[]`=全禁；`[a,b]`=白名单 |
| `allowed_skill_paths` | `None / [] / ["dir/"]` | `None` | `None` | 前缀匹配过滤 skill 目录 |

`SkillTool.to_openai_schema()` 会在 `function.description` 里自动附带当前 agent 可见的 skill 目录（`- weather: ...`）——LLM 通过 function schema 就能看到"能调哪些 skill"，所以 messages 里不需要重复贴一份清单。

---

#### 2.3.5 哪些会落盘 / 哪些只是一次性便签

| 在 messages 里出现的东西 | SQLite | transcripts |
| --- | --- | --- |
| S1~S7（所有 system 消息） | ❌ | ❌ |
| 历史 user/assistant/tool（来自上次 load） | ✅（本来就在库里） | ✅ |
| 本轮 user_query | ✅ 一进来就 `_commit` | ✅ |
| 每轮 LLM 吐的 assistant | ✅ 每条立刻 `_commit` | ✅ |
| 每次工具产出的 tool | ✅ 每条立刻 `_commit` | ✅ |
| autocompact 生成的那条 user 摘要 | ❌（只在本轮 messages 工作副本里） | ❌ |

**铁律**：`role == "system"` 的消息一律**不进 SQLite**（`SessionStore._VALID_ROLES = {user, assistant, tool}`）。否则历史里会混进**过期的** env_info / hook 上下文，下次 submit 就乱套了。

**write-ahead 语义**：任何一条 user/assistant/tool 消息在**产生的那一刻**就写 SQLite + transcripts。进程被 `SIGKILL` 最多丢"LLM 正在流式吐的那段还没提交的文本"；下次 load 时孤儿 `tool_calls` 会被 `filter_unresolved_tool_uses` 自动过滤掉。

**压缩 vs 磁盘**：压缩作用在**本轮 messages 工作副本**上（给 LLM 看）；磁盘永远是完整记录。下次 submit 重新从 SQLite 拉完整历史、重新装 messages、再次可能触发压缩。SQLite 有 FIFO（`max_history_messages` 默认 400），transcripts 不截（只进不出，供审计）。

---

#### 2.3.6 常见定制速查

| 想做的事 | 改什么 |
| --- | --- |
| 某次调用换身份（节点模式，像 quant_agent） | `AgentLoop.run_*(system_prompt="...")` → 替换 [A]，跳过 [E] |
| 永久换所有 agent 身份 | 改 `agent/roles/parts.py::BASE_IDENTITY` |
| 只改 explore 行为 | 改 `definitions/explore/AGENT.md` body（= [B]） |
| 让 env_info 每轮更新时间戳 | 注册 `before_llm` hook，每轮改 `messages[0]["content"]` |
| 注入 session 级动态上下文 | 注册 `session_start` hook，返回 `{"additional_context": "..."}` → 变 S3 |
| 禁 explore 的 MCP | frontmatter 加 `allowed_mcp_servers: []` |
| 让 general 只能看到 `finance/` 下的 skill | `allowed_skill_paths: [finance/]` |
| 关掉 env_info | agent 的 `include` 里删掉 `env_info` |
| 只留 [A]（极简） | `AgentConfig(name="...", include=[PromptPart.BASE_IDENTITY], ...)` |
| 做长期待办提醒 | 用 `tools/task.py` 写 todo；下次 submit 的 S4 会自动露出来 |
| 不落盘 SQLite（纯内存跑） | `QueryEngine(store=None)` |
| 改压缩阈值 | `ContextManager(auto_compact_ratio=0.85, hard_limit_ratio=0.95, ...)` |

---

### 2.4 工具可见性：谁能用哪些工具

**位置**：`agent/roles/registry.py::ToolRegistry.schemas(agent_config)`

三个 agent 字段逐层过滤，最终得到发给 LLM 的 `tools: [...]`：

| 字段 | 类型 | general 默认 | explore/plan/verify 默认 | 未设置时 |
| --- | --- | --- | --- | --- |
| `allow_write_tools` | bool | `true` | `false`（砍 bash/write/edit/notebook_edit/task_*） | `true` |
| `allowed_mcp_servers` | `None / [] / [names...]` | `None`（全开） | `None` | `None` = 全开；`[]` = 全禁；`[a,b]` = 白名单 |
| `allowed_skill_paths` | `None / [] / ["dir/"...]` | `None` | `None` | 同上，前缀匹配 |

`tool_references`（上一节的 D 块）会**反映这层过滤后的结果**——也就是说，给 explore 的 system prompt 里压根看不到 `bash` 的使用指引。

---

### 2.5 主循环：`while step < max_turns`

**位置**：`agent/loop.py::run_stream`

```
每一轮 = 一次 LLM call + 0..N 次工具执行
─────────────────────────────────────────
Step 1  ContextManager 压缩        agent/compaction.py
         • microcompact  始终：截 30min+ 前重型工具结果
         • autocompact   token ≥ 0.85 → LLM 异步 9 段结构化摘要替换老消息
         • hard_limit    token ≥ 0.95 → 头尾强截（兜底）
         • circuit breaker: autocompact 连败 3 次 → 只 micro + hard
         • yield CompactionEvent（UI「↻ 已压缩 N→M」）
Step 2  Hook before_llm           可改 {messages, tools, model}
Step 3  LLM 流式调用               llm/llm_client.py::stream_with_tools
                                   逐 chunk yield TextDeltaEvent
                                   收尾 postprocess + token 统计
Step 4  Hook after_llm            可改 {content, tool_calls}
★ 若本轮没有 tool_calls → break → ⑥ 收尾
Step 5  死循环检测                 agent/executor.py::doom_loop_check
                                   最近 3 轮 tool_calls 指纹相同 → 抛错
Step 6  PermissionManager 8 步     permissions/manager.py
                                   bypass / pause / session-cache /
                                   allow-set / glob-rule / bash→BashGuard /
                                   写路径 / 指纹化 denial / ASK / fallback
Step 7  Hook before_tool           可改 args
Step 8  ToolExecutor 并发/串行     agent/executor.py
                                   • 只读 tool → asyncio.gather
                                   • 写 tool  → pending group 串行
                                   • BuiltinTool → tools/<name>.py::execute
                                   • MCP tool    → mcp/client.py::call_tool
                                   • subagent    → tools/subagent.py 派生新 AgentLoop
                                   • 超长结果外置 .runtime/tool_results/
                                   • 副效应：recently_read / invoked_skills / todo_list
Step 9  Hook after_tool            可改 result.content
────────────────────────────────────────
       ★ 每产出一条 user / assistant / tool 消息
         → on_message_commit(msg) → QueryEngine._commit_message(sid, msg)
           → 内存 append + transcripts 追加 + SQLite 追加
       ★ append 到本轮 messages → step++ → 回 Step 1
```

**压缩"膨胀"澄清**：压缩针对**本轮喂给 LLM 的工作副本**，磁盘 (SQLite + transcripts) 永远是完整记录。下一次 submit 会再从磁盘拉完整历史、再装一遍、再压缩。SQLite 有 FIFO（`max_history_messages`），transcripts 不截（只进不出，供审计）。

---

### 2.6 收尾

**位置**：`agent/loop.py` 末尾 + `agent/query_engine.py::submit` 后半段

- AgentLoop yield `DoneEvent`（本轮增量消息、token 用量、工具列表）
- **所有消息早已在 Step 8 / 9 `_commit_message` 里落盘**，这里只是把最终文本回给 UI 流式输出
- **Verify Gate**（可选）：派生 `verify` 子 agent
  - `block`：判 FAIL → 阻断本次输出
  - `repair`：判 FAIL → 加一回合让主 agent 修复，新 assistant 经 `replace_session` 整体回写保证内存/磁盘一致
- `record_last_session(sid)`：写 `~/.whalefall/runtime/state/last_session.txt`
- 全程旁路落盘已写好：`.runtime/traces/`（TraceWriter）、`.runtime/transcripts/`（已在 Step 8 落）、`.runtime/state/sessions.sqlite3`（已在 Step 8 落）、`.runtime/artifacts/`

---

### 2.7 一眼能看穿的不变量

| 不变量 | 由谁保证 | 单测 |
| --- | --- | --- |
| system 角色消息**永不进 SQLite** | `SessionStore._VALID_ROLES` | `test_history_never_contains_system_role` |
| 历史里**永不出现孤儿 tool_calls** | `SessionStore.filter_unresolved_tool_uses` @ load | `test_orphan_tool_calls_dropped_on_load` |
| 静态前缀**字节稳定跨 turn** | `render_system_prompt_split` 只把 env_info 扔进 dynamic | `test_static_prefix_byte_stable_across_calls` |
| split + merge ≡ render | `render_system_prompt = static + "\n\n" + dynamic` | `test_split_merge_matches_render_system_prompt` |
| **零文件嗅探**（cwd 下 md 永远不会被框架自动读） | loader 只读 `definitions/`，不看 cwd | `test_render_system_prompt_does_not_read_any_file` |
| 续接会话**不重建历史** | `_ensure_session_loaded` 只在内存空时 load；submit 只尾部追加 | （基于 `append_message` 的 session_store 测试覆盖） |

---

### 2.8 常见定制速查

| 想做的事 | 改哪里 | 会不会影响其它 agent |
| --- | --- | --- |
| 给 `general` 加一段"全部用简体中文"的项目规范 | 调用 `AgentLoop.run_*(system_prompt="# 规范\n...")` | 否（只影响本次调用） |
| 不想要框架默认身份，用我自己的完整提示词（节点模式，像 `quant_agent/common/`） | 同上；`system_prompt` 非空时会**整体替换 `base_identity` 并跳过 `env_info`** | 否 |
| 改所有 agent 的默认身份 | 改 `agent/roles/parts.py::BASE_IDENTITY` | **是**（所有 agent） |
| 给 explore 加专属行为 | 改 `definitions/explore/AGENT.md` body | 否 |
| 禁 explore 的 MCP | `allowed_mcp_servers: []` 加到 explore 的 frontmatter | 否 |
| 让 general 只能看到 `finance/` 下的 skill | `allowed_skill_paths: [finance/]` | 否 |
| 关掉 `env_info` 里的时间 cwd | 在对应 agent 的 `include` 里去掉 `env_info` | 否 |
| 不落盘 SQLite（纯内存跑） | `QueryEngine(store=None)` | 否 |
| 改压缩阈值 | `ContextManager(auto_compact_ratio=0.85, hard_limit_ratio=0.95, ...)` | 所有进程内 AgentLoop |


---

## 三、完整项目树

```
src/whalefall/
├── main.py                      ← CLI 入口；解析 --agent/--model/--web 等并装配组件
├── README.md                    ← 本文件
│
├── .runtime/                    ← 所有运行态（WHALEFALL_RUNTIME_DIR 可覆盖）
│   ├── logs/                    ← 结构化日志
│   ├── traces/YYYY-MM-DD/       ← 每次 request 一个 <rid>.jsonl
│   ├── artifacts/               ← web_browser 截图等产物
│   ├── tool_results/            ← 超长工具结果外置存放
│   ├── state/                   ← sessions.sqlite3 + state.db
│   └── transcripts/             ← 完整对话 transcript
│
├── agent/                       ← 运行时核心（对齐 CC 的 query.ts / QueryEngine.ts）
│   ├── loop.py                  ← AgentLoop 主循环 + system_prompt 5 层装配
│   ├── query_engine.py          ← QueryEngine：session 持久化 + Verify Gate
│   ├── executor.py              ← ToolExecutor：并发调度 + doom_loop_check
│   ├── compaction.py            ← ContextManager：三层压缩 + circuit breaker
│   ├── hooks.py                 ← HookManager：8 种 hook 事件（比 CC 更广）
│   ├── events.py                ← TextDelta / ToolStart / ToolEnd / Compaction / Done
│   └── roles/                   ← Agent 定义系统
│       ├── config.py            ← AgentConfig + WRITE_TOOLS + DEFAULT_INCLUDE
│       ├── parts.py             ← PromptPart 枚举 + 静态常量 + 动态渲染
│       ├── loader.py            ← 扫 AGENT.md → AgentConfig + render_system_prompt
│       └── definitions/
│           ├── general/AGENT.md      ← 通用（100 turns，全工具，可派生子 agent）
│           ├── explore/AGENT.md      ← 只读探索（80 turns）
│           ├── plan/AGENT.md         ← 规划（50 turns，无 MCP）
│           ├── verify/AGENT.md       ← 独立验证（40 turns，输出 VERDICT）
│           └── echo-tester/AGENT.md  ← 端到端自检 custom 示例
│
├── tools/                       ← 16+ 内建工具（都继承 BuiltinTool）
│   ├── base.py                  ← BuiltinTool / ToolContext / ToolResult
│   ├── registry.py              ← ToolRegistry + build_default_registry
│   ├── read.py                  ← 读文件，offset/limit；记 recently_read 供压缩恢复
│   ├── write.py                 ← 整文件写，自动建父目录
│   ├── edit.py                  ← old/new 精准替换，支持 replace_all
│   ├── notebook_edit.py         ← Jupyter 结构化编辑（tmp+rename 原子写，失败回滚）
│   ├── glob.py                  ← glob 匹配，按修改时间排序
│   ├── grep.py                  ← ripgrep 包装（content / files_with_matches / count）
│   ├── bash.py                  ← 子进程 shell；BashGuard 先做静态分类
│   ├── fetch.py                 ← web_fetch：URL → Markdown
│   ├── web_search.py            ← SearXNG 优先 / DDG 后备
│   ├── web_browser.py           ← Playwright 浏览器（导航 / 截图到 .runtime/artifacts）
│   ├── skill.py                 ← 按需加载 skills/<path>/SKILL.md；分层前缀过滤
│   ├── subagent.py              ← `agent` 工具：派生子 Agent（同步 / 后台 job）
│   ├── todo.py                  ← task_create / update / get / list（看板模式）
│   ├── plan_mode.py             ← enter_plan_mode / exit_plan_mode（只规划不执行）
│   ├── ask.py                   ← ask_user_question（CLI/Web 结构化多选）
│   ├── sleep.py                 ← 阻塞等待（测试 / 节流）
│   └── config.py                ← 列出当前模型别名与 llm_config 概要
│
├── permissions/                 ← 权限管道（对齐 CC 的 5 步，扩展为 8 步）
│   ├── manager.py               ← PermissionManager 8 步 + 指纹化 denial
│   └── bash_guard.py            ← BashGuard：shlex 分段 + 路径归一化 + DANGER/WARN 正则
│
├── storage/                     ← 所有落盘入口集中于此
│   ├── session_store.py         ← SessionStore：SQLite + per-session 锁 + BEGIN IMMEDIATE
│   ├── trace.py                 ← TraceWriter：JSONL 逐条写；失败不抛
│   └── retention.py             ← RuntimeRetention：LRU + TTL + 容量三重策略
│
├── llm/                         ← LLM 接入层
│   ├── llm_client.py            ← LLMClient 门面（call_llm / stream_with_tools / ...）
│   ├── config.py                ← get_model_info / get_model_context（读 ini 别名）
│   ├── gateway/
│   │   ├── clients.py           ← make_sync/async_client + normalize_base_url + 缓存
│   │   └── response.py          ← ChatCompletion 解包 + 业务错误识别（success=false / status_code<0）
│   ├── postprocess/
│   │   ├── json_cleaner.py      ← 从杂乱输出里挖 JSON（fence 剥离 / 引号修补 / 平衡括号）
│   │   ├── text_cleaner.py      ← 去高频页眉页脚 + 关键字截断尾部声明
│   │   └── tokens.py            ← TokenUtils：tiktoken 包装（count / truncate / head_tail）
│   └── config/
│       ├── llm_config.ini.example  ← 模型别名模板（复制为 llm_config.ini 后填 key）
│       └── llm_config.ini       ← 本机敏感配置（.gitignore 不入库）
│
├── mcp/                         ← MCP 协议层 + 本机演示服务端
│   ├── client.py                ← MCPClient：stdio / sse / http；MCP annotations 识别写工具
│   ├── config.yaml.example      ← MCP server 连接配置模板
│   ├── config.yaml              ← 本机配置（.gitignore 不入库）
│   ├── server/
│   │   ├── app.py               ← FastMCP 单例（import plugins 即向其注册 @tool）
│   │   └── __main__.py          ← python -m whalefall.mcp.server
│   └── plugins/
│       └── hello.py             ← 演示插件：echo / add / time_now，用于跑通链路
│
├── skills/                      ← 分层技能目录（SKILL.md 文档，按 domain 嵌套）
│   ├── general/                 ← 通用
│   │   └── weather/SKILL.md     ← wttr.in / Open-Meteo 的查询小抄
│   └── demo/                    ← 演示嵌套结构（用来展示 allowed_skill_paths 过滤）
│       └── nested/
│           ├── alpha/SKILL.md
│           └── beta/SKILL.md
│
├── ui/
│   ├── cli.py                   ← InteractiveCLI：Rich 终端 UI；/help /exit /clear /compact 等
│   ├── web.py                   ← FastAPI + WebSocket；默认 non-interactive
│   ├── streaming.py             ← StreamHandler + CompactionRecord（UI 侧缓存）
│   ├── slash/
│   │   ├── core.py              ← normalize_slash_input / parse_slash / SlashContext /
│   │   │                          dispatch_common → /clear /compact /resume /init /stats
│   │   └── __init__.py
│   └── static/index.html        ← 单页前端（WS 客户端）
│
├── core/
│   ├── log.py                   ← Timer / get_logger / request_id 绑定 / truncate
│   └── runtime.py               ← runtime_root / logs_dir / traces_dir / state_dir
│
└── tests/                       ← pytest 回归套件
    ├── test_bash_guard.py       ← DANGER/WARN/SAFE 用例 + 路径归一化
    ├── test_permissions.py      ← 默认集合互斥 + 指纹化 denial
    ├── test_executor.py         ← doom_loop JSON 兜底 + 不同参数不误判
    ├── test_notebook_edit.py    ← 原子写 + 写失败回滚
    ├── test_session_store.py    ← 多线程并发 append 不丢消息
    ├── test_llm_postprocess.py  ← JSON 清洗 / 长文清洗 / token 截断
    ├── test_skill_filter.py     ← 分层 skill 前缀过滤 + 运行期拒绝
    ├── test_slash.py            ← /clear /compact /resume /init /stats 分发
    └── test_roles_e2e.py        ← 端到端：agent 加载 + prompt 渲染 + schema 过滤
```

---

## 四、Agent 类型

五个 Agent 都是同一种写法 —— `agent/roles/definitions/<name>/AGENT.md`。
内建 agent 没有"特权"：想改 `general` 的提示词就直接改那个 `.md`；新增 agent 建个新目录即可。
`--agent` CLI 选项动态读取 `list_agent_names()`，新加 custom agent **不改代码立即可用**。

| 类型 | max_turns | 写工具 | 子 Agent | MCP | Skill | AGENT.md 正文 | 定位 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `general` | 100 | ✅ | ✅ | ✅ 全看 | ✅ 全看 | ❌（空 body） | 通用入口，全权限 |
| `explore` | 80 | ❌ | ❌ | ✅ 全看 | ✅ 全看 | ✅ | 只读代码库探索 |
| `plan` | 50 | ❌ | ❌ | ❌ `[]` | ✅ 全看 | ✅ | 方案设计 / 步骤拆解 |
| `verify` | 40 | ❌ | ❌ | ✅ 全看 | ✅ 全看 | ✅ | 独立复核，输出 VERDICT |
| `echo-tester` | 10 | ❌ | ❌ | ❌ `[]` | ❌ `[]` | ✅ | 端到端自检 custom 示例 |

### 4.1 AGENT.md 单文件配置

```markdown
---
name: factor-verify
description: 从数据源独立复核因子计算逻辑
model: gpt-4.1                                     # 可选，覆盖 CLI --model
max_turns: 60
allow_write_tools: false                           # 只读（registry 自动过滤写工具）
allow_subagent: false                              # 禁止再派生子 agent
allowed_mcp_servers: [quant]                       # 可选，限定可见 MCP server
allowed_skill_paths: [general/, finance/stock/]    # 可选，限定可见 skill 前缀
include: [base_identity, env_info, system_prompt, guardrails, tool_references]
---

[Factor Verify] 从数据源独立复核因子计算：
- 不相信前序 agent 的结论；
- 用三个独立指标交叉验证；
- 输出 JSON：{pass: bool, evidence: [...], disagree_with_prev: [...]}
```

### 4.2 权限与过滤字段（只有 4 个）

| 字段 | 类型 | 默认 | 语义 |
| --- | --- | --- | --- |
| `allow_write_tools` | `bool` | `true` | 写工具（bash / write / edit / notebook_edit）总闸 |
| `allow_subagent` | `bool` | `true` | `agent` 工具开关（是否允许再派生子 agent） |
| `allowed_mcp_servers` | `list \| None` | `None` | MCP server 白名单（三态） |
| `allowed_skill_paths` | `list \| None` | `None` | skill 路径前缀白名单（三态） |

三态语义（两个 `allowed_*` 字段共享，对齐 CC 默认"全看"）：

| 配置 | 语义 |
| --- | --- |
| 字段不写 / `None` | 全看 |
| `[]` | 全禁 |
| `[x, y]` | 白名单 |

`allowed_skill_paths` 匹配规则（路径相对 `skills/` 根，形如 `general/weather`）：

- 以 `/` 结尾 → **目录前缀**（含嵌套）：`[general/]` 放行 `general/` 下所有 skill
- 不以 `/` 结尾 → **精确匹配**：`[finance/stock/backtest]` 只放行这一个
- **list 端 + execute 端双端校验**：LLM 看不到被禁 skill，即使直接猜到路径也 load 不了

---

## 五、每个 Agent 的系统提示词（5 层装配 + static/dynamic 切分）

`include` 字段声明使用哪些积木，按顺序拼为最终 system prompt。实现位置：
`agent/roles/parts.py`（积木常量 + 动态渲染）+ `agent/roles/loader.py::render_system_prompt()`。

```text
[静态前缀：字节稳定，prompt cache 友好]
Layer 1: base_identity        ← 通用身份 + 核心行为准则（parts.py::BASE_IDENTITY）
Layer 2: system_prompt        ← 各 Agent AGENT.md 正文（agent 独有；`definitions/<name>/AGENT.md` body）
Layer 3: guardrails           ← 诚实与执行约束（写操作前置检查）
Layer 4: tool_references      ← 已注册每个 BuiltinTool.prompt() 汇总；附 [工具使用指引] 标题

[动态后缀：每次渲染重算，放末尾不破坏前缀缓存]
Layer 5: env_info             ← 日期 / cwd / 平台 / 斜杠命令提示
```

> 要注入任务级的额外指令，用 `AgentLoop.run_*(system_prompt=...)` 传完整 markdown
> —— 它会整体替换 Layer 1（BASE_IDENTITY），并**自动跳过** Layer 5
> （调用方自己掌控上下文）。对应的内部形参叫 `custom_base`。
>
> `render_system_prompt_split(agent)` 会直接返回 `(static_prefix, dynamic_suffix)`
> 两段式。想做 provider 端 prompt cache 断点（如 Anthropic `cache_control`），
> 就把静态前缀做为一个块、动态后缀做为下一个块即可——字节稳定性由单测保证。

**general** — Layer 1 + 2 + 3 + 4 + 5（Layer 2 为空，通用场景不需要专属指令）。

**explore** — Layer 2（`system_prompt` body）内容（节选）：

```text
[Explore Mode] 只读探索模式，专注代码库搜索与分析。
- 使用 glob / grep / read 遍历文件；并发发起所有无依赖的查询。
- 不执行任何写操作（write/edit/bash 写命令均被屏蔽）。
- 给出精确的文件路径和行号，方便调用方直接定位。
- 若发现多个可能答案，全部列出并注明置信度。
- 不要编造结论——如确实找不到答案，明确说明未找到及已查范围。
```

**plan** — Layer 2 内容（节选）：

```text
[Plan Mode] 规划模式，专注方案设计与步骤拆解。
- 先分析需求与约束，再制定分步实施方案。
- 不执行任何代码，不写文件；仅输出规划文档。
- 每一步说明：目标、前置条件、具体操作、预期结果、潜在风险。
- 在方案末尾列出不确定假设，并给出验证方法。
- 方案应清晰到可直接交由执行 Agent 操作，不留模糊地带。
```

**verify** — Layer 2 内容（节选）：

```text
[Verify Mode] 对抗性独立验证模式。
- 不可依赖前序 Agent 的结论；必须从数据源独立复核。
- 从三个维度检验：① 数据完整性 / ② 逻辑自洽性 / ③ 边界条件。
- 发现矛盾时，明确指出哪一步推理有误，并提供证据（文件路径 + 行号）。
- 不修改任何文件，只输出验证报告。
- 必须以如下格式结尾：
    VERDICT: PASS / FAIL / PARTIAL
    理由：[具体说明，含参考文件路径和行号]
```

---

## 六、内建工具（16+）

所有内建工具继承 `tools/base.py::BuiltinTool`：

- `name` 唯一工具名；`read_only` 决定是否并发 + 是否走 ASK 管道
- `parameters_schema` 返回 OpenAI function-calling schema
- `execute(ctx, args)` 实际执行
- `prompt()` 返回该工具在 system prompt 中的使用指引（被 Layer 4 `tool_references` 自动汇总）

| 工具 | R/W | 用途 |
| --- | --- | --- |
| `read` | R | 读文件（offset/limit 分段；记 recently_read 供压缩恢复） |
| `write` | W | 整文件写，自动建父目录 |
| `edit` | W | `old_string / new_string` 精准替换，支持 `replace_all` |
| `notebook_edit` | W | Jupyter 结构化编辑（tmp+rename 原子写，失败回滚） |
| `glob` | R | glob 模式找文件，按修改时间排序 |
| `grep` | R | ripgrep 包装，三种输出模式 |
| `bash` | W | 子进程 shell；经 BashGuard 预检 |
| `web_fetch` | R | URL → Markdown（正文抓取） |
| `web_search` | R | SearXNG 优先，DDG 后备 |
| `web_browser` | R | Playwright（导航 / 截图；落 `.runtime/artifacts/web/`） |
| `skill` | R | 按名加载 `skills/<path>/SKILL.md`；分层前缀过滤 |
| `agent` | R | **子 Agent 触发器**（同步 / `background=true` 后台 job） |
| `task_create/update/get/list` | W | TodoWrite 风格任务看板 |
| `enter_plan_mode / exit_plan_mode` | R | 只规划不执行模式开关 |
| `ask_user_question` | R | 结构化多选问答（CLI/Web 都有 UI） |
| `sleep` | R | 阻塞等待（测试 / 节流） |
| `config` | R | 列出模型别名与 llm_config 概要 |

`ToolRegistry.schemas(agent_config=...)` 按 `allow_write_tools` 过滤写工具。
`ToolRegistry.is_write_tool_by_name(name)` 是运行时权威判定（优先于静态 `WRITE_TOOLS`）。

---

## 七、权限 8 步管道

`permissions/manager.py::PermissionManager.check_batch()`：

```text
Step 1   bypass_all         → ALLOW               （--dangerously-bypass）
Step 1.5 pause_all          → DENY                （--pause 全阻塞）
Step 2   session 级缓存      → always_allow / always_denied
Step 3   白名单集合          → ALLOW
         DEFAULT_ALLOW_TOOLS = {read, glob, grep, agent, skill,
                               web_search, task_*, plan_mode, ...}
Step 4   Glob 精细规则       → ALLOW               （fnmatch 匹配参数值）
Step 5   bash → BashGuard
           DANGER           → DENY                （直接拒绝，不询问）
           WARN             → ASK（附警告）
Step 6   写工具路径约束      → DANGER=DENY         （write / edit / notebook_edit）
Step 6.5 指纹化 denial       → 同 (tool, md5(args)) 连续 3 次拒绝 → 自动 DENY
Step 7   写工具 / ask_tools  → ASK                 （开启询问）
Step 8   其它                → ALLOW
```

**默认集合互斥**：`DEFAULT_ALLOW_TOOLS ∩ DEFAULT_ASK_TOOLS = ∅`（由 `test_permissions.py` 守护），
不会出现"既允许又询问"。**Web UI 默认 non-interactive**，所有 ASK 被静默 DENY；完全放行需 `WHALEFALL_WEB_BYPASS=1`。

### 7.1 BashGuard（`permissions/bash_guard.py`）

- `shlex.split` 分段，支持 `;`、`&&`、`||`、`|`、`$(...)`、反引号
- `rm` 类命令取 `_basename`，检查 `-rf` 目标是否为 `/ /* ~ ~/ /root /home` 或 resolve 后为根/家
- 正则扫 DANGER / WARN 模式（fork bomb、远程 `curl | sh`、`dd of=/dev/…`、`mkfs`、`shutdown`、`iptables -F`、`crontab -r`…）
- `is_protected_path(path)` 检查 `raw / normpath / resolve()` 三种候选，且 macOS 上 `/etc/passwd` 与 `/private/etc/passwd` 视为同一条目

---

## 八、会话、上下文压缩、死循环检测

### 8.1 QueryEngine（会话级）

- 启动时从 `.runtime/state/sessions.sqlite3` 按 `session_id` 载入历史（自动过滤孤儿 tool_calls）
- `submit(query, sid)`：
  1. 先 `append_message(sid, user_msg)` 立刻落盘 + 写 transcripts
  2. 传 `extra_messages=已有历史` 调 `AgentLoop.run_stream`，并把 `on_message_commit=_commit_message` 作为回调
  3. `AgentLoop` 每产出一条 assistant / tool 消息就回调一次 `_commit_message`，走同一套 append + transcripts 通道
  4. 成功收尾时 `record_last_session(sid)`，供 `--resume-last` / `/resume-last` 续场
- 同一个 `session_id` 多次 submit = 尾部追加，不会重建 system prompt 中的历史（system 每轮都动态重渲染，历史永远只在 user/assistant/tool）
- 跨请求触发 `RuntimeRetention.run()` 清理旧运行态
- **Verify Gate**（`WHALEFALL_VERIFY_GATE_MODE=off|block|repair`）
  - `block`：verify 子 agent 判 FAIL → 本次输出被阻断
  - `repair`：FAIL → 自动加一回合让主 agent 修复，这条改写后的 assistant 用 `replace_session` 整体回写以保证 SQLite 与内存一致

### 8.2 ContextManager 三层压缩（全异步）

| 层 | 触发 | 动作 |
| --- | --- | --- |
| `microcompact` | 始终尝试；老工具结果超 8k 或 30min 前 | 截断重型工具（read/write/edit/bash/glob/grep/web_*/notebook_edit/quant__*）的结果字段 |
| `autocompact` | token 占比 ≥ **0.85** context window | 用 LLM 异步生成结构化 `<summary>`（意图 / 已完成 / 当前状态 / 涉及文件…）替换老消息；保留最近 6 轮完整 |
| `hard_limit` | token 占比 ≥ **0.95** | 强制截断（兜底） |

Circuit breaker：autocompact 连续失败 3 次 → 停止 LLM 摘要，只做 micro + hard。
压缩后恢复：AgentLoop 自动回填 `recently_read`（文件）/ `invoked_skills` / `todo_list` 的新鲜内容。

### 8.3 死循环检测（`agent/executor.py::doom_loop_check`）

最近 3 轮 tool_calls 的指纹（`md5(name::sorted(args))`）完全相同 → 抛 `RuntimeError`。
JSON 解析失败自动 fallback 到 `str(raw_args)`，不会默默放过；**不同参数不会误判**（由 `test_executor.py` 守护）。

---

## 九、子 Agent（`agent` 工具）

- 同进程内 spawn 一个新的 `AgentLoop`（用子 agent 自己的 `AgentConfig`）
- 父 agent 把子 agent 的 **最终消息** 作为 tool_result 接回来
- 可选 `background=true`：扔后台线程跑，父 agent 立刻拿到 `job_id`，后续用 `wait_seconds` 短轮询取结果
  - 超时由 `WHALEFALL_AGENT_BG_TIMEOUT` 控制（默认 30s，上限 600s）
- 子 agent 可通过 frontmatter 的 `allow_subagent: false` 关闭再嵌套
- `subagent_start` hook 可为子 agent 注入额外上下文

---

## 十、Hook 生命周期（8 种事件）

`agent/hooks.py::HookManager`（线程安全）支持 8 种 hook，比 CC 的 hook 管道更广：

| 事件 | 载荷 | 返回可改 |
| --- | --- | --- |
| `session_start` | `{session_id, messages}` | messages |
| `before_llm` | `{messages, tools, model}` | 全部 |
| `after_llm` | `{content, tool_calls}` | 全部 |
| `before_tool` | `{tool_name, args, ctx}` | args |
| `after_tool` | `{tool_name, args, content, is_error}` | content |
| `on_error` | `{exc, traceback}` | ✗（只读日志） |
| `subagent_start` | `{subagent_name, messages}` | messages |
| `tool_use_failure` | `{tool_name, reason}` | ✗（监控） |

默认注册 `on_error` 日志 hook；其它留给用户扩展。

---

## 十一、Storage（统一落盘）

所有落盘入口集中在 `storage/`，位置都在 `.runtime/`（或 `last_session.txt` 在 `~/.whalefall/runtime/state/`）：

- `session_store.py::SessionStore` — write-ahead 会话存储
  - 表结构：`sessions(session_id, updated_at, created_at)`
    + `session_messages(session_id, ordinal, role, content, tool_calls_json, tool_call_id, tool_name, ts)`
    —— **每条消息一行**，按 ordinal 严格有序
  - 核心 API：
    - `append_message(sid, msg)` / `append_messages(sid, msgs)` — 单条 / 批量追加（**默认路径**，每次产出一条立刻写）
    - `replace_session(sid, msgs)` — 原子重写（`save_session` 现在是它的别名，供压缩回写）
    - `load_session(sid)` — 读出后自动跑 `filter_unresolved_tool_uses`：**assistant.tool_calls 没有匹配 tool_result 的整条丢弃、没有匹配 assistant 的 tool 也丢弃**，对齐 Claude Code 的 `filterUnresolvedToolUses`。崩溃恢复时自然跳过没写完的一轮
  - `_VALID_ROLES = {user, assistant, tool}` —— **system 永不落盘**（system 每次 submit 由 `render_system_prompt` 重新生成）
  - 老库兼容：首次打开发现 `sessions.messages_json` 列，会自动拆成每行一条消息迁移到新表，然后 DROP 掉旧列
  - per-session `threading.Lock` + `BEGIN IMMEDIATE`，8 线程 × 20 条并发不丢消息
  - 位置：`.runtime/state/sessions.sqlite3`
- `transcripts.py::append_transcript` — 全量对话档案
  - SQLite 受 `max_history_messages` FIFO 约束（默认 400 条），但 transcripts 是**只进不出**的 JSONL
  - `append_transcript(sid, msg)` 每条消息旁路写 `.runtime/transcripts/<sid>.jsonl`
  - 审计 / 复盘场景下想看原始完整对话就翻这里
- `last_session.py::record_last_session / read_last_session` — 最近活跃会话
  - 一个字符串文件 `~/.whalefall/runtime/state/last_session.txt`（可用 `WHALEFALL_LAST_SESSION_FILE` 覆盖）
  - 供 CLI `--resume-last` 与斜杠命令 `/resume-last` 使用；每次 `QueryEngine.submit` 成功时顺手更新
- `trace.py::TraceWriter`
  - JSONL：每次 request 开 `.runtime/traces/YYYY-MM-DD/<rid>.jsonl`
  - 记录 `llm_call / tool_run / compaction / done`
  - 失败不抛，`_logger.warning` 记账
- `retention.py::RuntimeRetention`
  - LRU + TTL + 容量三重策略
  - 覆盖 `logs / traces / artifacts / tool_results / transcripts`
  - `QueryEngine` 每 N 次请求触发一次（`WHALEFALL_RETENTION_RUN_EVERY`，默认 50）

### 11.1 Write-ahead 流水线

```text
[submit]              [AgentLoop.run_stream]                 [QueryEngine]
   │                       │                                    │
   │── user_query ────▶  _commit(user_msg) ───────────────▶ on_message_commit
   │                       │                                    │
   │                   LLM stream …                             │
   │                       │                                    │
   │                   assistant + tool_calls                   │
   │                   _commit(assistant_tool) ─────────▶ append_message → SQLite + transcripts
   │                       │                                    │
   │                   exec tool_calls                          │
   │                   _commit(tool_result) ──────────▶ append_message → SQLite + transcripts
   │                       │                                    │
   │                   …repeat…                                 │
   │                       │                                    │
   │                   final assistant                          │
   │                   _commit(final_msg) ─────────────▶ append_message → SQLite + transcripts
   │                       │                                    │
   │◀──── final text ──────┘                                    │
   │                                                            │
   │                                   record_last_session(sid) │
   └────────────────────────────────────────────────────────────┘
```

这套流水线等价于 Claude Code 的 log-writer：**assistant 文本吐到哪里就持久化到哪里**，
崩溃/断电最多丢解码缓冲里还没提交的一小段 delta，下次加载自动过滤孤儿 tool_calls。

---

## 十二、MCP 层

### 12.1 配置

加载顺序（`mcp/client.py::_load_config`）：

1. **显式传入** `MCPClient(config_path=...)` —— 文件必须存在，否则抛错
2. **环境变量** `WHALEFALL_MCP_CONFIG=/abs/path` —— 同上，文件必须存在
3. **包内默认路径** `src/whalefall/mcp/config.yaml` —— 存在则读
4. **都没有** —— 自动回退到内建 demo 配置（stdio 起 `python -m whalefall.mcp.server`，提供 `echo / add / time_now`）。这样 `pip install whalefall` 后无需复制任何模板文件即可跑通链路。

想接入自定义 MCP server，参考 `config.yaml.example`：

```yaml
servers:
  demo:
    type: stdio                            # stdio | sse | http
    command: python
    args: ["-m", "whalefall.mcp.server"]
    env:
      PYTHONPATH: <PROJECT_ROOT>/src
    description: 内置演示 MCP server（echo / add / time_now）
```

### 12.2 本地 MCP Server（`python -m whalefall.mcp.server`）

1. 初始化 `mcp/server/app.py::mcp = FastMCP(...)`
2. 挨个 import `mcp.plugins.*` 模块（模块 import 即向 `mcp` 注册 @tool）
3. stdio 起 server，等待 MCPClient 建连

默认只带一个 `hello.py` 演示插件（`echo` / `add` / `time_now`）。要扩展，在
`mcp/plugins/` 下新建模块并在 `mcp/server/app.py` 里 import 它即可。

### 12.3 MCPClient（`mcp/client.py`）

- `connect()`：按 config 起 server，拉 `list_tools`
- `list_tools(servers=[...])`：返回 OpenAI schema 列表（已前缀 server 名，如 `quant__get_stock_data`）
- `call_tool(name, args)`：路由到对应 server；失败自动重连一次
- `is_destructive(name)`：基于 MCP annotations 判断是否写工具（与 `AgentConfig.allow_write_tools` 协作）
- `max_result_chars` 裁剪超长结果；事件 loop 独立后台线程跑

---

## 十三、LLM 层

### 13.1 门面（`llm/llm_client.py::LLMClient`）

保留历史公共 API：`call_llm / call_llm_async / stream_with_tools / count_tokens / truncate_by_tokens / truncate_head_tail / _clean_json / clean_main_text`，
内部全部委托给 `gateway/` 与 `postprocess/`。

### 13.2 Gateway（`llm/gateway/`）

- `clients.py`：`normalize_base_url`（去尾 `/`）、`client_cache_key`、`make_sync_client` / `make_async_client`（带 OpenAI client 复用 LRU）
- `response.py`：`completion_first_message()` 解包 `ChatCompletion`，识别 "HTTP 200 但网关返回 success=false / status_code<0" 这类业务错误

### 13.3 Postprocess（`llm/postprocess/`）

- `json_cleaner.py`：从 LLM 杂乱输出里挖 JSON（code fence 剥离 / 未转义引号修补 / 平衡括号）
- `text_cleaner.py`：去高频页眉页脚 + 按关键字截断尾部声明（适合研报 / 公告 PDF）
- `tokens.py::TokenUtils`：`tiktoken cl100k_base` 封装（`count / truncate / truncate_head_tail`，头 0.7、尾其余，中间插入 `[... 中间内容已截断 ...]`）

### 13.4 模型配置

`llm/config/llm_config.ini`：每个模型一组 `*_model` / `*_url` / `*_context` / `*_key`。
CLI `--model` 接受这里的别名（如 `gpt-4o-mini`、`deepseek-v3`、`qwen-max` 等）。
默认模型写在 `main.py` 里（开源版默认 `gpt-4o-mini`）。模板见 `llm_config.ini.example`。

---

## 十四、UI

### 14.1 CLI（`ui/cli.py`）

- readline 历史落在 `.runtime/state/`
- 流式输出（`StreamHandler`）；`Rich` 不可用时降级纯文本
- 启动参数：
  - `--session-id <sid>`：用指定 id 启动 / 接续
  - `--resume-last`：读 `~/.whalefall/runtime/state/last_session.txt`，接续上一次没聊完的那个 session
- 斜杠命令：
  - 共享实现 `ui/slash/`：`/clear` `/compact` `/resume [id]` `/resume-last` `/sessions` `/init` `/stats` `/help`
  - CLI 专属：`/exit` `/model <alias>` `/agent <name>`

### 14.2 Web（`ui/web.py`）

- FastAPI + WebSocket，前端单页 `ui/static/index.html`
- 默认 `host=0.0.0.0 port=8000`
- 权限：默认 **non-interactive**；`WHALEFALL_WEB_BYPASS=1` 全放行（仅本机调试）
- 冷启动：`WHALEFALL_WEB_COLD_START=1` → 刷新即新 session，`/resume` 被禁用
- 背压：WS 并发发送限制 `WHALEFALL_WS_MAX_PENDING_SENDS=128`
- 并发安全：`_abort_events` 有独立锁，`_send` 不在 send_lock 内阻塞取 result
- **顶栏重载按钮**：
  - 🔄 `POST /api/reload`：软重载 — 重建 LLM / MCP / QueryEngine，重读 `llm_config.ini` 与 `mcp/config.yaml`；进程不重启，WS 不断；**Python 代码改动不生效**
  - ♻️ `POST /api/restart`：硬重启 — `os.execv(sys.executable, [sys.executable] + sys.argv)` 自替换，保留启动参数；WS 断 3~5 秒后自动重连；**所有 Python 代码改动生效**
  - `GET /health` 返回 `{ok, reloading, mcp_tool_count, model, error}`，供前端轮询判断服务是否就绪

### 14.3 斜杠命令共享实现（`ui/slash/core.py`）

| API | 作用 |
| --- | --- |
| `normalize_slash_input(q)` | 全角 `／` → `/`、去零宽字符、trim |
| `parse_slash(text)` | 返回 `(command, arg)`；非斜杠输入 `("", text)` |
| `SlashContext` | `{query_engine, session_id, strict_cold_start, extra_stats_fn, cwd}` |
| `SlashResult` | `{handled, message, cleared, should_exit}` |
| `dispatch_common(text, ctx)` | 分发 `/clear /reset /compact /resume /init /stats`；未命中返回 `handled=False` |
| `format_session_list` | `/resume` 无参时的会话列表格式化 |

---

## 十五、对照 Claude Code 架构图的覆盖情况

`whalefall` 以 Claude Code（CC）为参照蓝图，多数核心能力已对齐或做了简化/扩展：

| CC 特性 | 状态 | 实现位置 |
| --- | --- | --- |
| QueryEngine 主循环 | ✅ | `agent/loop.py` + `agent/query_engine.py` |
| buildSystemPrompt（5 层） | ✅ 对齐 | `agent/loop.py::_build_system_prompt` + `roles/parts.py`（static 前缀 + dynamic 后缀） |
| AGENT.md 向上扫描 | ❌ 明确弃用 | 整体替换 Layer 1 `BASE_IDENTITY`：`AgentLoop.run_*(system_prompt=...)`。**零文件嗅探**：框架永不读取 cwd。 |
| 项目级提示词（单独一层） | ❌ 已删除 | 合并到 `system_prompt` 整体替换方案，减少一层参数心智负担 |
| Prompt cache 切分（static/dynamic） | ✅ | `roles/loader.py::render_system_prompt_split` —— 前缀字节稳定，尾部只放时间/cwd |
| Write-ahead 持久化 | ✅ | `SessionStore.append_message` + `transcripts.append_transcript`（每条消息立刻落盘） |
| 孤儿 tool_calls 过滤 | ✅ 对齐 `filterUnresolvedToolUses` | `SessionStore.filter_unresolved_tool_uses` |
| `--resume-last` / 最近会话记忆 | ✅ | `storage/last_session.py` |
| 权限 5 步管道 | ✅ 简化+扩展 | `permissions/manager.py`（8 步，跳过 LLM 判断，新增 pause/指纹） |
| YOLO_MODE（bypass） | ✅ | `bypass_all` |
| **PAUSE_MODE** | ✅ 新增 | `pause_all` + `PermissionManager.pause_mode()` |
| BashClassifier | ✅ | `permissions/bash_guard.py` |
| yoloClassifier（LLM 判断） | ❌ 跳过 | 太复杂，无必要 |
| Level 1 microCompact | ✅ | `agent/compaction.py` |
| **Level 2 sessionMemoryCompact** | ✅ 新增 | `compaction.py` 结构化 JSON（9-section 摘要） |
| Level 3 autoCompact / hardLimit | ✅ | `agent/compaction.py` |
| Swarm / subAgent | ✅ 简化 | `tools/subagent.py::AgentTool`（同进程 spawn） |
| SpeculationEngine | ❌ 跳过 | 本项目暂无必要 |
| 工具系统（16+ 个） | ✅ | `tools/*.py` |
| **EnterPlanMode / ExitPlanMode** | ✅ 新增 | `tools/plan_mode.py` |
| AskUserQuestion | ✅ | `tools/ask.py` |
| MCP 动态注册 | ✅ | `mcp/client.py`（stdio / sse / http） |
| 工具元信息：`is_destructive` | ✅ | `mcp/client.py`（MCP annotations） |
| 后端连接失败自动重试 | ✅ | `mcp/client.py`（call_tool 失败自动重连一次） |
| Hook 生命周期 | ✅ 更广 | `agent/hooks.py`（8 种事件，覆盖全生命周期） |
| 会话持久化 | ✅ | `storage/session_store.py` + `.runtime/state/` |
| 容量治理 | ✅ | `storage/retention.py` + `session_store.enforce_limits()` |
| 跨 agent Skill 体系 | ✅ 简化+扩展 | `tools/skill.py` + `skills/`（分层前缀过滤，对齐 CC"默认全看"） |
| **server_instructions（MCP 级提示）** | ❌ 删除 | 一个 server 足够，独立工具 docstring 够用 |

---

## 十六、运行

### 16.1 CLI

```bash
cd src
python -m whalefall.main                              # 交互模式
python -m whalefall.main "列出本目录下所有 python 文件并统计行数"  # 单次
python -m whalefall.main --agent explore "搜索 *.ipynb"
python -m whalefall.main --agent plan "重构因子回测流程"
python -m whalefall.main --agent verify "复核这份分析"
python -m whalefall.main --model gpt-4.1 --no-stream "…"
python -m whalefall.main --bypass "…"                 # 危险：跳过所有权限询问
python -m whalefall.main --no-mcp --no-builtin "…"

# 续接上次会话
python -m whalefall.main --resume-last                # 读 last_session.txt
python -m whalefall.main --session-id <sid>           # 指定会话 id 接续
```

交互模式斜杠命令（`/resume-last` / `/sessions` / `/resume <sid>`）也可运行时切换会话。

想给模型一套项目级指令？直接把完整的 system prompt 作为字符串或 markdown 传给 Python API：

```python
from whalefall import AgentLoop
from pathlib import Path

loop = AgentLoop(llm, tools, {})
text = loop.run(
    "检查这个项目的潜在 bug",
    system_prompt=Path("./my_task.md").read_text(encoding="utf-8"),
)
```

`system_prompt` 会整体替换 Layer 1 `BASE_IDENTITY`，并自动跳过 Layer 5 `ENV_INFO` —— 上下文完全由调用方掌控。**框架永远不会自动去 cwd 读取任何 `*.md`**。

### 16.2 Web

```bash
python -m whalefall.main --web --host 0.0.0.0 --port 8000
```

Web UI 通过浏览器 `localStorage` 自动记住并续接最后一次会话 id；侧栏的「会话」列表可一键切换。想给某次 session 注入项目级指令，走上面的 Python API 路径或 `/init` 生成 `AGENTS.md` 作为团队约定文档。

### 16.3 MCP Server 单跑

```bash
python -m whalefall.mcp.server                        # stdio；由 MCPClient 拉起即可
```

### 16.4 测试

```bash
cd src/whalefall
python -m pytest tests/ -q                            # 全量回归
python tests/test_roles_e2e.py                        # 端到端：agent + prompt 自检
```

### 16.5 Python API（嵌入）

```python
from whalefall.agent.roles import (
    AgentConfig, PromptPart,
    load_agents, list_agent_names, get_agent,
    render_system_prompt, render_system_prompt_split,
    is_write_tool, WRITE_TOOLS,
)

list_agent_names()          # ['echo-tester', 'explore', 'general', 'plan', 'verify']
cfg = get_agent("explore")  # 找不到自动回退 general
sp = render_system_prompt(cfg, registry=build_default_registry())

static_prefix, dynamic_suffix = render_system_prompt_split(
    cfg, registry=build_default_registry()
)
# static_prefix 字节稳定，适合插 Anthropic cache_control 断点

sp_custom = render_system_prompt(
    cfg,
    registry=build_default_registry(),
    custom_base="# 项目规范\n全部使用简体中文回答",
)
```

更高层接入推荐用 `QueryEngine`，它走的就是 write-ahead 通道：

```python
from whalefall.agent.query_engine import QueryEngine

qe = QueryEngine(...)
async for chunk in qe.submit(
    "帮我读 README.md",
    session_id="demo",
):
    print(chunk, end="")
```

整段对话（user / assistant / tool）会在产生的那一刻写进 `sessions.sqlite3` 与 `.runtime/transcripts/demo.jsonl`，进程被 SIGKILL 也能恢复。

---

## 十七、环境变量

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `WHALEFALL_RUNTIME_DIR` | 覆盖 `.runtime/` 根目录 | `src/whalefall/.runtime` |
| `WHALEFALL_MCP_CONFIG` | 覆盖 MCP 配置路径 | `src/whalefall/mcp/config.yaml` |
| `WHALEFALL_AGENT_BG_TIMEOUT` | 子 agent 后台 job 取结果超时 | `30`（上限 600） |
| `WHALEFALL_VERIFY_GATE_MODE` | `off / block / repair` | `off` |
| `WHALEFALL_RETENTION_RUN_EVERY` | QueryEngine 每 N 次请求触发清理 | `50` |
| `WHALEFALL_RETENTION_TRACES_MAX_FILES` | trace 最多保留文件数 | `1000` |
| `WHALEFALL_RETENTION_TRACES_MAX_BYTES` | trace 总大小上限 | 1 GB |
| `WHALEFALL_RETENTION_ARTIFACTS_MAX_BYTES` | artifact 总大小上限 | 1 GB |
| `WHALEFALL_RETENTION_TOOL_RESULTS_MAX_BYTES` | 外置 tool_results 总大小上限 | 1 GB |
| `WHALEFALL_RETENTION_TRANSCRIPTS_MAX_BYTES` | transcripts 总大小上限 | 1 GB |
| `WHALEFALL_RETENTION_LOGS_MAX_BYTES` | logs 总大小上限 | 1 GB |
| `WHALEFALL_WEB_BYPASS` | Web UI 全量放行 | `0` |
| `WHALEFALL_WEB_COLD_START` | Web UI 冷启动（禁 `/resume`） | `0` |
| `WHALEFALL_WS_MAX_PENDING_SENDS` | WS 背压队列 | `128` |
| `WHALEFALL_LAST_SESSION_FILE` | 覆盖 `last_session.txt` 位置 | `~/.whalefall/runtime/state/last_session.txt` |
| `SEARXNG_URL` | `web_search` 优先使用的 SearXNG | `http://localhost:8080` |
| `MCP_LOG_STDOUT` | MCP server 日志到 stdout | `0` |

---

## 十八、可选依赖

- **web_search**
  - 优先 SearXNG：本机跑 `searxng-docker` 或设 `SEARXNG_URL`
  - 后备 DDG：`pip install ddgs`
- **web_browser**：`pip install playwright && playwright install chromium`
- **MCP**：已由 FastMCP 支持 stdio/sse/http，无额外依赖
- **tiktoken**：压缩与 `TokenUtils` 依赖

---

## 十九、设计原则（写给自己的）

1. **单一事实源**
   - 写工具真伪：`BuiltinTool.read_only` / `MCPClient.is_destructive`（`WRITE_TOOLS` 仅启动保底）
   - Agent 定义：AGENT.md 单文件，内建 agent 无特权
2. **事实与规则分离**
   - 规则（BASE / GUARDRAILS）静态常量；事实（env / tool prompts）动态渲染
3. **CLI / Web / 后台 job 走同一个 `QueryEngine`**，session 与压缩逻辑不分叉
4. **所有落盘入口收敛到 `storage/`**，所有运行态收敛到 `.runtime/`，不散布 `open()`
5. **Permission 8 步管道是显式、可读、可单测的**；默认集合互斥由 pytest 守护
6. **所有 agent / skill / MCP 插件都在 `src/whalefall/` 内**——不做 CC 那种项目级/用户级多源加载
7. **不给"日常助手"做长期记忆偏好**：记忆迭代由 skill 体系（SOP 文档）承载，而不是隐式地学习用户习惯，确保每次 agent 的输出都是可审计的。
