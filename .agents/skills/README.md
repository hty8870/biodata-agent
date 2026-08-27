# BioData Agent skills

On-demand procedures for working on this repository. Each skill is a thin router into the authoritative `AGENTS.md`, `docs/agent/`, and `MODULES.md` sources: invoke the one that matches your task instead of reading every document.

| Skill | Invoke when |
|---|---|
| [biodata-change-lifecycle](biodata-change-lifecycle/SKILL.md) | About to edit any code, data, config, doc, or governance file: the safe-change flow from first write to merge. |
| [biodata-windows-python](biodata-windows-python/SKILL.md) | About to run Python, pytest, the quality gate, the frozen evaluation, or git on this Windows / non-ASCII-path repository. |
| [biodata-frontend-contract](biodata-frontend-contract/SKILL.md) | About to rename or reshape an `/api` response field, MCP output, or shared frontend global. |
| [biodata-release-readiness](biodata-release-readiness/SKILL.md) | Pre-merge checks, CI-parity checks, release-candidate packaging, gate-failure diagnosis, or rollback planning. |

Format: each skill directory has `SKILL.md` (minimal `name` and `description` front matter, then the routing procedure), an optional `references/` folder one level deep, and an optional `agents/openai.yaml`. All are validated by `tests/test_project_skill.py`.
