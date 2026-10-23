# BioData Agent skills

On-demand procedure libraries for working with this project. A skill is a directory here holding
`SKILL.md`: minimal `name` / `description` front matter, then the procedure an agent should follow.
A directory may also carry an optional `references/` folder (one level deep) and an optional
`agents/openai.yaml`. Every skill is validated by `tests/test_project_skill.py`.

## Shipped with the product

| Skill | Invoke when |
|---|---|
| [biodata-dataset-discovery](biodata-dataset-discovery/SKILL.md) | An agent is answering dataset-discovery questions **using BioData Agent as a product** (its MCP tools or web API) and must relay the catalog's honesty guarantees faithfully. |

## Developer-side skills

This repository additionally carries skills for agents that **change this codebase** — the backend
and frontend coding constraints, the change lifecycle, the HTTP/frontend contract, release
readiness, the Windows/Python environment, project-context retrieval, experiment-arm methodology,
and the offline data pipeline. Those skills describe internal collaboration procedures, so they are
classified as internal: the public mirror and the customer delivery exclude them
(`.deliveryignore`). They *are* part of the release-candidate archive, because that archive is the
reviewable-source artifact for trusted reviewers (`scripts/build_release.py` collects them by
design; audience is recorded in its manifest). Names above are given without links for that reason.

## Format

`SKILL.md` front matter carries exactly `name` and `description` — no tool-permission front matter,
so no skill can pre-approve a shell command for itself. Every skill in this library is validated by
`tests/test_project_skill.py`.
