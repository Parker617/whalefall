---
description: Demo skill "alpha" — placeholder SKILL.md showcasing arbitrarily nested skill layouts.
---

# Demo Alpha

Placeholder skill at `skills/demo/nested/alpha/`. Whalefall follows the Claude Code
"Agent Skills" convention: every `SKILL.md` under `src/whalefall/skills/` is indexed
into the system prompt at submit time, and the agent loads the body via the `read`
tool when it decides the skill is relevant.

## What a real SKILL.md should contain

- YAML front-matter (`description:` minimum — this is what the index shows to the LLM)
- Step-by-step procedure, do's and don'ts, canned prompts
- Links to code or data the agent should consult

Replace this file with real SOP content when you want an actual skill.
