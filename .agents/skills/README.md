# BioData Agent skills

On-demand procedure libraries for this repository. Currently one skill ships:

| Skill | Invoke when |
|---|---|
| [biodata-dataset-discovery](biodata-dataset-discovery/SKILL.md) | An agent is answering dataset-discovery questions **using BioData Agent as a product** (its MCP tools or web API) and must relay the catalog's honesty guarantees faithfully. |

Format: each skill directory has `SKILL.md` (minimal `name` and `description` front matter, then the routing procedure), an optional `references/` folder one level deep, and an optional `agents/openai.yaml`. All are validated by `tests/test_project_skill.py`.
