---
name: biodata-change-lifecycle
description: Run a BioData Agent change safely from first write to merge: triage read-only vs write, create a claim, keep cross-module contracts atomic, respect the frozen baseline and secret red lines, match the quality gate to the risk, and record one dev-log entry. Use before editing any code, data, config, doc, or governance file in the BioData Agent repository.
---

# BioData change lifecycle

Make the change minimal, evidence-first, and reversible. This is the on-demand version of `AGENTS.md` section 3 (lifecycle), section 2 (red lines), and the `docs/agent/` companions; read those for the authoritative wording. Full ordered checklist and claim skeleton: [references/change-checklist.md](references/change-checklist.md).

## 1. Triage before touching anything

- **Read-only** (inspect, analyze, diagnose, report): do it, but create no claim, dev-log, cache, or temp file.
- **Write** (anything that could change version-controlled content): run section 2 before the first write.
- If a read-only task turns into a write, stop and switch to the write flow first.

## 2. Before the first write (in order, no skipping)

1. `git status --short`; identify and preserve unrelated changes.
2. Read the companion docs your change triggers (`AGENTS.md` section 8 routing table): COLLABORATION and LOGGING for any write; ARCHITECTURE_AND_CONTRACTS and the real `MODULES.md` for backend, frontend, API, MCP, or schema; QUALITY_GATES for tests or delivery; WINDOWS_ENVIRONMENT for running commands.
3. Check `协同/认领/` and `协同/交接/`; confirm no one holds the files you will touch.
4. Fix branch, target base_ref, and single-writer isolation. Parallel writers need separate `git worktree`s; solo sequential work on one feature branch is fine.
5. Create your own claim `协同/认领/<tool>-<task-slug>-<short-id>.md` (solo too). State scope, out-of-scope, expected files, contract flag, planned gates, branch, base_ref, worktree.

## 3. While writing

- Touch only the claimed scope. Keep the diff minimal; do not refactor unrelated code.
- A **cross-module contract** change (HTTP response, MCP tool input/output, CLI, data record fields, Python public API, frontend shared globals or localStorage keys, script-parsed formats) needs `contract_change: compatible | breaking` in the claim, and producer plus every consumer plus tests plus docs must land **atomically**. Breaking contracts never merge half-done. For `/api` fields or shared JS bindings use `$biodata-frontend-contract`.
- **Red lines (never cross):** `database/base/` is the frozen 767-record baseline, so do not modify it or write uploads into it; uploads go only to `database/external/`; never read, print, log, commit, or deliver secrets; no `git reset --hard` or destructive checkout; unpublished or internal material never enters public delivery.

## 4. Before delivering

- Run the gates that match the risk (how to run them: `$biodata-windows-python`). Query, retrieval, ranking, or base changes must keep the frozen recommendation evaluation exactly at the official baseline.
- Re-read the diff for secrets, caches, temp files, unrelated edits, or unexpected baseline drift.
- Prepend one entry to `开发日志归档/开发日志.md`: what, why, effect, files, validation run and results, status, branch, contract, open handoffs.

## 5. Stop and report when

No verifiable Python 3.10+; ambiguous ownership of overlapping changes; a gate fails or a frozen baseline changes; a contract cannot land atomically; a secret or unpublished material would be exposed; or the task needs authority beyond what the user granted. Stopping is not quitting: keep evidence, state the exact blocker and the smallest next step.
