# Whalefall 🐋

> A local **LLM agent harness**: drive any OpenAI-compatible model through built-in tools, MCP plugins, skill documents, and subagents — in a single process, with honest permissions and full trace persistence.

**Whalefall** (鲸落, "whale-fall") is a deep-sea phenomenon: when a giant whale sinks to the seabed, its body sustains an entire ecosystem of scavengers and bone-eating worms for decades. This project takes that metaphor literally — **one big language model underwrites a swarm of smaller tool calls, keeping an agent productive turn after turn**.

Inspired by the overall shape of [Claude Code](https://github.com/anthropics/claude-code), rewritten from scratch in pure Python, with every moving piece inspectable and every side-effect explicit.

---

## Highlights

- **Single-process main loop** — `LLM → tool_calls → tool_results → next turn`, fully async streaming, every chunk landed to disk via `TraceWriter`.
- **16+ built-in tools** — `read / write / edit / bash / glob / grep / web_fetch / web_search / ask / todo / notebook_edit / agent / plan_mode / skill / mcp_discover / ...`, covering the CC feature matrix with ~85% of the functionality.
- **First-class MCP support** — stdio / SSE / streamable-HTTP; plugins self-register via `@mcp.tool()`; your tools can live in a private fork without forking this repo.
- **Hierarchical skill filtering** — markdown SOP docs under `skills/`; agents pick what they can see via `allowed_skill_paths` (path prefixes with proper `/` boundary semantics).
- **Subagents** — the `agent` tool spawns a child loop with its own permissions/context/MCP subset; parent auto-summarizes child transcripts for traceability.
- **8-step permission pipeline** — hook / bypass / skip / always-allow / rule / mode / deny / prompt; explicitly-declared write tools need user approval unless bypassed.
- **BashGuard** — an `ll`-lite classifier that flags destructive `rm -rf /`, pipes to `sh`, hidden network calls, etc. before the shell sees them.
- **Triple-layer context compression** — `microcompact` (truncate old tool results), `auto_compact` (summarize after 92% of context), `precompact` (eager summary before the next turn if projected to overflow).
- **Write-ahead persistence** — every user / assistant / tool message hits SQLite **the instant it's produced**, so a crash mid-tool at most loses the assistant text still in the decoder buffer; on reload, orphan `tool_calls` with no matching `tool_result` are filtered out so the next turn starts from a consistent state.
- **Full transcript archive** — alongside the capped SQLite history (FIFO'd when it exceeds `max_history_messages`), Whalefall appends every message to `.runtime/transcripts/<sid>.jsonl` which is never truncated — great for audit & replay.
- **Prompt-cache friendly layout** — `render_system_prompt_split()` separates the static prefix (identity + agent body + guardrails + tool blurbs) from the dynamic tail (date/cwd/env). Static bytes are stable across turns so Anthropic/OpenAI prefix caches can actually hit.
- **`--resume-last` / `/resume-last`** — CLI remembers the last active session id in `~/.whalefall/runtime/state/last_session.txt`; a single flag resumes it in place, matching what the Web UI already does via `localStorage`.
- **Web UI with live tool trace** — FastAPI + WebSocket; soft-reload config on the fly (`🔄`) or hot-replace code via `os.execv` (`♻️`) — no need to leave the browser.
- **Explicit system prompt** — the default identity can be swapped wholesale via `AgentLoop.run_*(system_prompt=...)`. **Zero filesystem sniffing**: nothing is auto-read from `cwd`; you always know exactly what the model sees.

See `src/whalefall/README.md` for the ~700-line design document.

---

## Quickstart

### 1. Install

```bash
git clone https://github.com/Parker617/whalefall.git
cd whalefall
pip install -e '.[web]'
```

### 2. Configure your LLM

```bash
cp src/whalefall/llm/config/llm_config.ini.example src/whalefall/llm/config/llm_config.ini
# Edit the file — paste in your OpenAI / DashScope / DeepSeek / Ollama key
```

### 3. (Optional) Configure MCP

**No config needed for first run.** If `src/whalefall/mcp/config.yaml` is absent, Whalefall auto-loads a built-in demo server (`echo` / `add` / `time_now`) so you can verify the wiring end-to-end.

To connect your own MCP servers (stdio / SSE / streamable-HTTP):

```bash
cp src/whalefall/mcp/config.yaml.example src/whalefall/mcp/config.yaml
# Replace <PROJECT_ROOT> with the absolute path of the repo,
# and add your own servers alongside or instead of `demo:`
```

To add a new built-in tool, drop a module into `src/whalefall/mcp/plugins/` and import it from `mcp/server/app.py`.

### 4. Run

```bash
# CLI one-shot
whalefall "list every python file under src/ and count them"

# Interactive REPL
whalefall

# Sub-agent modes
whalefall --agent explore "find every todo comment"
whalefall --agent plan    "design a migration from v1 to v2 of this schema"
whalefall --agent verify  "audit the analysis above"

# Pick up where you left off
whalefall --resume-last
# or in an interactive session: /resume-last  /sessions  /resume <sid>

# Web UI
whalefall --web --port 8000
# open http://localhost:8000
```

### 5. (Optional) Customize the system prompt

Whalefall does **not** scan any markdown file from `cwd` (no auto-discovery). The system prompt is assembled from each agent's `agent/roles/definitions/<name>/AGENT.md` body. Two ways to inject custom rules:

1. **Edit the agent definition.** Change the body of `definitions/general/AGENT.md`, or drop a new folder `definitions/my_project/AGENT.md` and call it via `whalefall --agent my_project ...`. This is the long-term choice.
2. **Replace the identity on a single call.** Pass a markdown string as `system_prompt=` when calling `AgentLoop.run_*()`; it replaces the default `BASE_IDENTITY` block and suppresses the auto-generated env info, leaving your markdown fully in control. Useful for workflow nodes where each step needs its own persona.

```python
from whalefall.agent.loop import AgentLoop
loop = AgentLoop(...)
answer = loop.run(
    user_query="...",
    system_prompt="# You are a data-quality auditor.\nReply with VERDICT: ...",
)
```

---

## Architecture at a glance

```
┌─────────────────────────────────────────────────────────────────┐
│  UI  ─  CLI  /  Web (FastAPI + WebSocket)  /  Python API        │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                   ┌────────────▼─────────────┐
                   │      QueryEngine         │  session + .runtime/ persistence
                   └────────────┬─────────────┘
                                │
                   ┌────────────▼─────────────┐
                   │       AgentLoop          │  main turn loop + 8 hook events
                   └────────┬──────┬──────────┘
              ┌─────────────┘      └──────────────┐
     ┌────────▼─────────┐            ┌────────────▼─────────────┐
     │     LLMClient    │            │    ToolDispatcher        │
     │ (openai async)   │            │  builtin + MCP + subagent│
     └──────────────────┘            └────────────┬─────────────┘
                                                  │
                                ┌─────────────────┼─────────────────┐
                       ┌────────▼─────┐  ┌────────▼──────┐  ┌───────▼────────┐
                       │ BuiltinTools │  │  MCPClient    │  │   AgentTool    │
                       │ (read/write/ │  │ (stdio/SSE/   │  │ (spawn child   │
                       │  bash/...)   │  │  http)        │  │  AgentLoop)    │
                       └──────────────┘  └───────────────┘  └────────────────┘
```

---

## How does Whalefall compare?

| Feature | Whalefall | Claude Code | SmolAgents | LiteLLM |
|---|---|---|---|---|
| Local stateful main loop | ✅ | ✅ | partial | ❌ |
| Built-in file / bash tools | ✅ (16+) | ✅ | partial | ❌ |
| MCP (stdio + SSE + HTTP) | ✅ | ✅ | ❌ | ❌ |
| Subagents (`agent` tool) | ✅ | ✅ | ✅ | ❌ |
| Skill markdown documents | ✅ | ✅ | ❌ | ❌ |
| Permission pipeline | ✅ (8 steps) | ✅ | ❌ | ❌ |
| Web UI with live trace | ✅ | ✅ | ❌ | ❌ |
| OpenAI-compatible (works with Ollama, DeepSeek, etc.) | ✅ | ❌ | ✅ | ✅ |
| Language | Python | TypeScript | Python | Python |
| Runs offline against local LLM | ✅ | ❌ | ✅ | ✅ |

Whalefall isn't trying to replace Claude Code — CC is more polished and has deeper IDE integration. Whalefall's niche is **you own every byte of the loop**: you can read the whole thing in an afternoon, patch any sharp edge, and run it against whichever model you choose.

---

## Documentation

- **Design reference**: `src/whalefall/README.md` — full architecture walkthrough, agent type reference, system-prompt layers, CC parity table.
- **Examples**: `src/whalefall/tests/` — every public module has an end-to-end test that doubles as usage documentation.
- **Skill authoring**: `src/whalefall/skills/general/weather/SKILL.md` for a real example; `src/whalefall/skills/demo/nested/` for directory-layout patterns.

---

## Status

Whalefall is **alpha**. APIs may change, but the on-disk format of `.runtime/` is intended to be forward-compatible. Bug reports and PRs welcome — especially from anyone who's written their own CC-style harness and wants to compare notes.

## License

[MIT](LICENSE) — use it, fork it, ship it, no warranty.

Inspired by Anthropic's [Claude Code](https://github.com/anthropics/claude-code) (MIT). No code copied; design cues and feature checklist derived from the public release.
