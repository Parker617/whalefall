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

主循环伪代码（`agent/loop.py::AgentLoop.run_stream`）：

```text
组装初始消息列表：
  system: base_identity + env_info                    ← Layer 1
  system: AGENT.md 项目配置                           ← Layer 2（可选）
  system: AgentType 专属 system_prompt                ← Layer 3
  system: 行为约束 + 工具规范汇总                      ← Layer 4/5
  system: skills 目录摘要                              ← 额外注入
  [历史消息 + extra_messages ...]
  user:   本次新消息

while step < max_turns:
  ┌─────────────────────────────────────────────────┐
  │ Step 1: 压缩检查（ContextManager）              │
  │   microcompact  截老工具结果（始终执行）         │
  │   autocompact   超阈值 LLM 生成结构化摘要替换    │
  │   hard_limit    兜底强制截断                    │
  │                                                │
  │ Step 2: Hook before_llm（可改 msg/tools/model）  │
  │ Step 3: LLM 流式调用（stream_with_tools）        │
  │          → yield TextDeltaEvent                │
  │ Step 4: Hook after_llm（可改 content/tool_calls）│
  │ Step 5: 无 tool_calls → break（纯文本回复）     │
  │                                                │
  │ Step 6: 死循环检查（最近 3 轮相同指纹 → 中断）   │
  │ Step 7: PermissionManager 8 步管道               │
  │ Step 8: Hook before_tool（可改工具参数）         │
  │ Step 9: ToolExecutor 执行                       │
  │          只读工具 asyncio.gather 并发           │
  │          写工具 pending group 串行              │
  │ Step 10: Hook after_tool（可改工具结果）         │
  │          yield ToolStart / ToolEnd              │
  │                                                │
  │ 把工具结果追加进 messages → step++              │
  └─────────────────────────────────────────────────┘

yield DoneEvent（携带本轮消息增量）

QueryEngine 回收：
  追加本轮增量到 _sessions[sid]
  upsert 写回 SQLite
  每 50 次触发 housekeeping（清过期 / 超量会话）
```

---

## 二、完整项目树

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

## 三、Agent 类型

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

### 3.1 AGENT.md 单文件配置

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

### 3.2 权限与过滤字段（只有 4 个）

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

## 四、每个 Agent 的系统提示词（5 层装配）

`include` 字段声明使用哪些积木，按顺序拼为最终 system prompt。实现位置：
`agent/roles/parts.py`（积木常量 + 动态渲染）+ `agent/roles/loader.py::render_system_prompt()`。

```text
Layer 1: base_identity        ← 通用身份 + 核心行为准则（parts.py::BASE_IDENTITY）
Layer 2: env_info             ← 日期 / cwd / 平台 / 斜杠命令提示（每次渲染重算）
Layer 3: agent_md             ← 项目级 `./AGENT.md`（从 cwd 读，`/init` 可一键生成；支持 @include path 递归 3 层）
Layer 4: system_prompt        ← 各 Agent AGENT.md 正文（agent 独有）
Layer 5: guardrails           ← 诚实与执行约束（写操作前置检查）
Layer 6: tool_references      ← 已注册每个 BuiltinTool.prompt() 汇总；附 [工具使用指引] 标题
```

**general** — 只有 Layer 1 + 2 + 3 + 5 + 6，Layer 4 为空（通用场景不需要专属指令）。

**explore** — Layer 4 extra（节选）：

```text
[Explore Mode] 只读探索模式，专注代码库搜索与分析。
- 使用 glob / grep / read 遍历文件；并发发起所有无依赖的查询。
- 不执行任何写操作（write/edit/bash 写命令均被屏蔽）。
- 给出精确的文件路径和行号，方便调用方直接定位。
- 若发现多个可能答案，全部列出并注明置信度。
- 不要编造结论——如确实找不到答案，明确说明未找到及已查范围。
```

**plan** — Layer 4 extra（节选）：

```text
[Plan Mode] 规划模式，专注方案设计与步骤拆解。
- 先分析需求与约束，再制定分步实施方案。
- 不执行任何代码，不写文件；仅输出规划文档。
- 每一步说明：目标、前置条件、具体操作、预期结果、潜在风险。
- 在方案末尾列出不确定假设，并给出验证方法。
- 方案应清晰到可直接交由执行 Agent 操作，不留模糊地带。
```

**verify** — Layer 4 extra（节选）：

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

## 五、内建工具（16+）

所有内建工具继承 `tools/base.py::BuiltinTool`：

- `name` 唯一工具名；`read_only` 决定是否并发 + 是否走 ASK 管道
- `parameters_schema` 返回 OpenAI function-calling schema
- `execute(ctx, args)` 实际执行
- `prompt()` 返回该工具在 system prompt 中的使用指引（被 Layer 6 `tool_references` 自动汇总）

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

## 六、权限 8 步管道

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

### 6.1 BashGuard（`permissions/bash_guard.py`）

- `shlex.split` 分段，支持 `;`、`&&`、`||`、`|`、`$(...)`、反引号
- `rm` 类命令取 `_basename`，检查 `-rf` 目标是否为 `/ /* ~ ~/ /root /home` 或 resolve 后为根/家
- 正则扫 DANGER / WARN 模式（fork bomb、远程 `curl | sh`、`dd of=/dev/…`、`mkfs`、`shutdown`、`iptables -F`、`crontab -r`…）
- `is_protected_path(path)` 检查 `raw / normpath / resolve()` 三种候选，且 macOS 上 `/etc/passwd` 与 `/private/etc/passwd` 视为同一条目

---

## 七、会话、上下文压缩、死循环检测

### 7.1 QueryEngine（会话级）

- 启动时从 `.runtime/state/sessions.sqlite3` 按 `session_id` 载入历史
- `send(query)` 把 user msg append 进 messages，驱动 `AgentLoop.run_stream`
- 跨请求触发 `RuntimeRetention.run()` 清理旧运行态
- **Verify Gate**（`WHALEFALL_VERIFY_GATE_MODE=off|block|repair`）
  - `block`：verify 子 agent 判 FAIL → 本次输出被阻断
  - `repair`：FAIL → 自动加一回合让主 agent 修复

### 7.2 ContextManager 三层压缩（全异步）

| 层 | 触发 | 动作 |
| --- | --- | --- |
| `microcompact` | 始终尝试；老工具结果超 8k 或 30min 前 | 截断重型工具（read/write/edit/bash/glob/grep/web_*/notebook_edit/quant__*）的结果字段 |
| `autocompact` | token 占比 ≥ **0.85** context window | 用 LLM 异步生成结构化 `<summary>`（意图 / 已完成 / 当前状态 / 涉及文件…）替换老消息；保留最近 6 轮完整 |
| `hard_limit` | token 占比 ≥ **0.95** | 强制截断（兜底） |

Circuit breaker：autocompact 连续失败 3 次 → 停止 LLM 摘要，只做 micro + hard。
压缩后恢复：AgentLoop 自动回填 `recently_read`（文件）/ `invoked_skills` / `todo_list` 的新鲜内容。

### 7.3 死循环检测（`agent/executor.py::doom_loop_check`）

最近 3 轮 tool_calls 的指纹（`md5(name::sorted(args))`）完全相同 → 抛 `RuntimeError`。
JSON 解析失败自动 fallback 到 `str(raw_args)`，不会默默放过；**不同参数不会误判**（由 `test_executor.py` 守护）。

---

## 八、子 Agent（`agent` 工具）

- 同进程内 spawn 一个新的 `AgentLoop`（用子 agent 自己的 `AgentConfig`）
- 父 agent 把子 agent 的 **最终消息** 作为 tool_result 接回来
- 可选 `background=true`：扔后台线程跑，父 agent 立刻拿到 `job_id`，后续用 `wait_seconds` 短轮询取结果
  - 超时由 `WHALEFALL_AGENT_BG_TIMEOUT` 控制（默认 30s，上限 600s）
- 子 agent 可通过 frontmatter 的 `allow_subagent: false` 关闭再嵌套
- `subagent_start` hook 可为子 agent 注入额外上下文

---

## 九、Hook 生命周期（8 种事件）

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

## 十、Storage（统一落盘）

所有落盘入口集中在 `storage/`，位置都在 `.runtime/`：

- `session_store.py::SessionStore`
  - SQLite + per-session `threading.Lock` + `BEGIN IMMEDIATE`
  - `save_session(sid, msgs)` 整表覆盖 / `append_messages(sid, new_msgs)` 读-改-写原子追加
  - 测试覆盖：8 线程 × 20 条并发不丢消息
  - 位置：`.runtime/state/sessions.sqlite3`
- `trace.py::TraceWriter`
  - JSONL：每次 request 开 `.runtime/traces/YYYY-MM-DD/<rid>.jsonl`
  - 记录 `llm_call / tool_run / compaction / done`
  - 失败不抛，`_logger.warning` 记账
- `retention.py::RuntimeRetention`
  - LRU + TTL + 容量三重策略
  - 覆盖 `logs / traces / artifacts / tool_results / transcripts`
  - `QueryEngine` 每 N 次请求触发一次（`WHALEFALL_RETENTION_RUN_EVERY`，默认 50）

---

## 十一、MCP 层

### 11.1 配置

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

### 11.2 本地 MCP Server（`python -m whalefall.mcp.server`）

1. 初始化 `mcp/server/app.py::mcp = FastMCP(...)`
2. 挨个 import `mcp.plugins.*` 模块（模块 import 即向 `mcp` 注册 @tool）
3. stdio 起 server，等待 MCPClient 建连

默认只带一个 `hello.py` 演示插件（`echo` / `add` / `time_now`）。要扩展，在
`mcp/plugins/` 下新建模块并在 `mcp/server/app.py` 里 import 它即可。

### 11.3 MCPClient（`mcp/client.py`）

- `connect()`：按 config 起 server，拉 `list_tools`
- `list_tools(servers=[...])`：返回 OpenAI schema 列表（已前缀 server 名，如 `quant__get_stock_data`）
- `call_tool(name, args)`：路由到对应 server；失败自动重连一次
- `is_destructive(name)`：基于 MCP annotations 判断是否写工具（与 `AgentConfig.allow_write_tools` 协作）
- `max_result_chars` 裁剪超长结果；事件 loop 独立后台线程跑

---

## 十二、LLM 层

### 12.1 门面（`llm/llm_client.py::LLMClient`）

保留历史公共 API：`call_llm / call_llm_async / stream_with_tools / count_tokens / truncate_by_tokens / truncate_head_tail / _clean_json / clean_main_text`，
内部全部委托给 `gateway/` 与 `postprocess/`。

### 12.2 Gateway（`llm/gateway/`）

- `clients.py`：`normalize_base_url`（去尾 `/`）、`client_cache_key`、`make_sync_client` / `make_async_client`（带 OpenAI client 复用 LRU）
- `response.py`：`completion_first_message()` 解包 `ChatCompletion`，识别 "HTTP 200 但网关返回 success=false / status_code<0" 这类业务错误

### 12.3 Postprocess（`llm/postprocess/`）

- `json_cleaner.py`：从 LLM 杂乱输出里挖 JSON（code fence 剥离 / 未转义引号修补 / 平衡括号）
- `text_cleaner.py`：去高频页眉页脚 + 按关键字截断尾部声明（适合研报 / 公告 PDF）
- `tokens.py::TokenUtils`：`tiktoken cl100k_base` 封装（`count / truncate / truncate_head_tail`，头 0.7、尾其余，中间插入 `[... 中间内容已截断 ...]`）

### 12.4 模型配置

`llm/config/llm_config.ini`：每个模型一组 `*_model` / `*_url` / `*_context` / `*_key`。
CLI `--model` 接受这里的别名（如 `gpt-4o-mini`、`deepseek-v3`、`qwen-max` 等）。
默认模型写在 `main.py` 里（开源版默认 `gpt-4o-mini`）。模板见 `llm_config.ini.example`。

---

## 十三、UI

### 13.1 CLI（`ui/cli.py`）

- readline 历史落在 `.runtime/state/`
- 流式输出（`StreamHandler`）；`Rich` 不可用时降级纯文本
- 斜杠命令：
  - 共享实现 `ui/slash/`：`/clear` `/compact` `/resume [id]` `/init` `/stats` `/help`
  - CLI 专属：`/exit` `/model <alias>` `/agent <name>`

### 13.2 Web（`ui/web.py`）

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

### 13.3 斜杠命令共享实现（`ui/slash/core.py`）

| API | 作用 |
| --- | --- |
| `normalize_slash_input(q)` | 全角 `／` → `/`、去零宽字符、trim |
| `parse_slash(text)` | 返回 `(command, arg)`；非斜杠输入 `("", text)` |
| `SlashContext` | `{query_engine, session_id, strict_cold_start, extra_stats_fn, cwd}` |
| `SlashResult` | `{handled, message, cleared, should_exit}` |
| `dispatch_common(text, ctx)` | 分发 `/clear /reset /compact /resume /init /stats`；未命中返回 `handled=False` |
| `format_session_list` | `/resume` 无参时的会话列表格式化 |

---

## 十四、对照 Claude Code 架构图的覆盖情况

`whalefall` 以 Claude Code（CC）为参照蓝图，多数核心能力已对齐或做了简化/扩展：

| CC 特性 | 状态 | 实现位置 |
| --- | --- | --- |
| QueryEngine 主循环 | ✅ | `agent/loop.py` + `agent/query_engine.py` |
| buildSystemPrompt 5 层 | ✅ | `agent/loop.py::_build_system_prompt` + `roles/parts.py` |
| AGENT.md 向上扫描 | ✅ | `roles/parts.py::load_project_agent_md`（支持 `@include path` 递归 3 层） |
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

## 十五、运行

### 15.1 CLI

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
```

### 15.2 Web

```bash
python -m whalefall.main --web --host 0.0.0.0 --port 8000
```

### 15.3 MCP Server 单跑

```bash
python -m whalefall.mcp.server                        # stdio；由 MCPClient 拉起即可
```

### 15.4 测试

```bash
cd src/whalefall
python -m pytest tests/ -q                            # 全量回归
python tests/test_roles_e2e.py                        # 端到端：agent + prompt 自检
```

### 15.5 Python API（嵌入）

```python
from whalefall.agent.roles import (
    AgentConfig, PromptPart,
    load_agents, list_agent_names, get_agent,
    render_system_prompt, is_write_tool, WRITE_TOOLS,
)

list_agent_names()          # ['echo-tester', 'explore', 'general', 'plan', 'verify']
cfg = get_agent("explore")  # 找不到自动回退 general
sp = render_system_prompt(cfg, registry=build_default_registry())
```

---

## 十六、环境变量

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
| `SEARXNG_URL` | `web_search` 优先使用的 SearXNG | `http://localhost:8080` |
| `MCP_LOG_STDOUT` | MCP server 日志到 stdout | `0` |

---

## 十七、可选依赖

- **web_search**
  - 优先 SearXNG：本机跑 `searxng-docker` 或设 `SEARXNG_URL`
  - 后备 DDG：`pip install ddgs`
- **web_browser**：`pip install playwright && playwright install chromium`
- **MCP**：已由 FastMCP 支持 stdio/sse/http，无额外依赖
- **tiktoken**：压缩与 `TokenUtils` 依赖

---

## 十八、设计原则（写给自己的）

1. **单一事实源**
   - 写工具真伪：`BuiltinTool.read_only` / `MCPClient.is_destructive`（`WRITE_TOOLS` 仅启动保底）
   - Agent 定义：AGENT.md 单文件，内建 agent 无特权
2. **事实与规则分离**
   - 规则（BASE / GUARDRAILS）静态常量；事实（env / agent_md / tool prompts）动态渲染
3. **CLI / Web / 后台 job 走同一个 `QueryEngine`**，session 与压缩逻辑不分叉
4. **所有落盘入口收敛到 `storage/`**，所有运行态收敛到 `.runtime/`，不散布 `open()`
5. **Permission 8 步管道是显式、可读、可单测的**；默认集合互斥由 pytest 守护
6. **所有 agent / skill / MCP 插件都在 `src/whalefall/` 内**——不做 CC 那种项目级/用户级多源加载
7. **不给"日常助手"做长期记忆偏好**：记忆迭代由 skill 体系（SOP 文档）承载，而不是隐式地学习用户习惯，确保每次 agent 的输出都是可审计的。
