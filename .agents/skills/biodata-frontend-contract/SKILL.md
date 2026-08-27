---
name: biodata-frontend-contract
description: Change a BioData Agent HTTP response field, MCP output, or shared frontend global without silently breaking the browser. The three quality gates cannot catch a dropped frontend contract, so consult the field-to-consumer map and update every consumer atomically, then verify in a real browser. Use before renaming or reshaping any /api response field or shared JS binding.
---

# BioData frontend contract

The classic silent failure in this project. Authoritative map: root `MODULES.md`, section "/api/recommend 响应字段 -> 前端消费点映射". Condensed map and verify steps: [references/field-consumer-map.md](references/field-consumer-map.md).

## Why the gates miss it

`scripts/web_smoke_test.py` only does static string checks on the JS; it never executes a line of it. Rename an `/api/recommend` field in `webapp.py` and miss one consumer, and that card position renders **silently blank** while all three gates stay green.

## Procedure

1. Before changing a response field's name or shape, open the `MODULES.md` field-to-consumer map and list every consumer file for that field.
2. Update the producer (`webapp.py`) plus every consumer JS module plus tests plus `MODULES.md` in one atomic change; set `contract_change` in the claim.
3. Grep for stragglers: `git grep -n "<field>" -- web/static/js/` (no git: PowerShell `Select-String -Path web/static/js/*.js -Pattern "<field>"`).
4. Verify in a **real browser**, not just Web smoke: load the page, run the query, and read the DOM and console at the field's rendered position.

## Frontend load invariants (do not break)

`core.js` loads first, `boot.js` last. No top-level code references a cross-module binding at load time (TDZ). No duplicate top-level `const` or `let` names across modules. A new module is inserted before `boot.js` and added to Web smoke. `localStorage` keys are a contract.

## Same discipline for the other cross-consumer contracts

MCP tool names, params, and output schema; CLI flags and exit codes; data record field names and upload format; and any dev-log, claim, or handoff format parsed by a script. Change producer and consumer together, atomically.
