# Field to consumer map (BioData Agent)

Authoritative and complete: root `MODULES.md`, section "/api/recommend 响应字段 -> 前端消费点映射". Keep that table as the single source of truth; this file is a triage summary.

## High-traffic response fields and where they render

| Field | Consumer JS | Purpose |
|---|---|---|
| `results[]` | core.js, search.js, results.js, facets.js | result cards main data |
| `results[].{dataset_name,species,tissue,disease,chemistry,platform,assay,sample_size,raw_data_status,published_date,source,url,download_url,dataset_uid,n_files,reason}` | cards.js (buildCard) | each card field; rename one and that slot goes blank |
| `query_constraints[]` | facets.js | matched hard-constraint chips |
| `resolution_status`, `clarification` | results.js, facets.js | empty-state routing |
| `facets[]`, `result_total` | facets.js | facet refinement |
| `relaxation_options[]` | results.js | zero-result one-click relax |
| `degraded_search` | results.js | unresolved-term abstain: "ignore these words and search anyway" suggestion; never auto-applied unless the LLM gate approves |
| `action_markers[]` | results.js | execution phrasing (打包 / 下载脚本 / 导出引文) — signposts the task-pack entry, never executes |
| `coverage_caveats[]`, `applied_lenient` | results.js | honest-degradation notice and include |
| `unused_query_terms[]` | results.js | N1: descriptor terms with no filterable dimension, shown "not used to filter" (read-only) |
| `search_trace`, `strategy` | results.js, shell.js | "what this search used" |
| `llm_response_used`, `provider` | results.js, search.js | LLM status and cache decision |

Other endpoints: `/api/files` and `/api/introduction` render through cards.js; `/api/datasets`, `/api/sources`, `/api/upload`, and POST `/api/diagnose` render through browse.js.

## Verify after any field change

1. `git grep -n "<field>" -- web/static/js/` returns no un-updated consumer.
2. Real browser: load, query, and confirm the field's slot renders (not blank), with 0 console errors.
3. Web smoke passes; necessary but not sufficient, since it does not execute JS.
