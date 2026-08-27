# Change checklist (BioData Agent)

Authoritative sources: `AGENTS.md` sections 2 to 3, `docs/agent/COLLABORATION.md`, `docs/agent/LOGGING.md`, `docs/agent/QUALITY_GATES.md`. This file is the actionable condensation.

## Before first write

- [ ] `git status --short`: unrelated changes identified and preserved.
- [ ] Companion docs for the touched area read (AGENTS.md section 8 table).
- [ ] `协同/认领/` and `协同/交接/` checked; target files unoccupied.
- [ ] Branch, base_ref, and worktree / single-writer isolation confirmed.
- [ ] Own claim created from `协同/认领/_模板.md`.

## Claim skeleton

- schema_version, claim_id, created_at, updated_at, agent, agent_instance, coordinator
- task (one line), status (ACTIVE to VERIFYING to READY_TO_MERGE), branch, base_ref, worktree
- contract_change: none | compatible | breaking
- Sections: 写入范围 / 明确不改 / 计划验证 / 依赖与冲突检查 / 当前进度

## Red lines (never cross)

- `database/base/` frozen 767 baseline: no content change, no uploads into it, unless the task is an authorized controlled re-baseline (then update the fingerprint constant, the count, and the eval thresholds in the same batch).
- Uploads land only in `database/external/`.
- Secrets (`.env*`) are never read, printed, logged, committed, or delivered.
- No `git reset --hard`, destructive checkout, or overwriting unrelated work.
- Unpublished or internal material never enters public delivery.

## Contract atomicity

Producer plus every consumer plus tests plus docs in one change. Compatible additions still verify the old consumers and the default path. Breaking changes never leave master in a half-migrated state.

## Before delivery

- [ ] Risk-matched gates run (see `$biodata-windows-python`); results and any skips recorded.
- [ ] Frozen recommendation evaluation unchanged from baseline (if query, retrieval, ranking, or base touched).
- [ ] Diff re-checked: no secrets, caches, temp files, unrelated edits, or baseline drift.
- [ ] One dev-log entry prepended to `开发日志归档/开发日志.md`.

## Merge (integrator)

- [ ] Claim READY_TO_MERGE; validation, dev-log, and handoffs checked.
- [ ] Full applicable gates re-run on the target branch; frozen metrics match baseline bit-for-bit.
- [ ] `scripts/logrotate.py` health check.
- [ ] Cleanup commit removes merged claims and accepted-then-closed handoffs; branch kept unless removal is explicitly authorized.
