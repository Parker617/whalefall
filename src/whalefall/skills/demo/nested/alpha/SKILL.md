---
description: Demo skill "alpha" — showcases nested skill directories and hierarchical allowed_skill_paths filtering.
---

# Demo Alpha

Placeholder skill at `skills/demo/nested/alpha/`, used to demonstrate how
Whalefall supports arbitrarily nested skill layouts plus per-agent filtering
via `allowed_skill_paths`.

## When this is visible to an agent

- `allowed_skill_paths=None` or missing — all skills visible (default)
- `allowed_skill_paths=["demo/"]` — this + every skill under `demo/`
- `allowed_skill_paths=["demo/nested/"]` — this + siblings under `nested/`
- `allowed_skill_paths=["demo/nested/alpha"]` — exact match, only this one
- `allowed_skill_paths=[]` — nothing visible

## What a real SKILL.md should contain

- YAML front-matter (`description:` minimum — that's what shows in the dynamic
  tool description)
- Step-by-step procedure, do's and don'ts, canned prompts
- Links to code or data the agent should consult

Replace this file with real SOP content when you want an actual skill.
