---
name: biodata-release-readiness
description: Audit BioData Agent changes, select and run the correct manifest-defined quality gates, build or verify an allowlisted release candidate, and prepare exact rollback evidence. Use for pre-merge checks, CI parity checks, release-readiness reviews, release-candidate packaging, failed-gate diagnosis, or rollback planning in the BioData Agent repository.
---

# BioData release readiness

Keep the work evidence-first and reversible.

## Route the task

1. Resolve the repository root and read `AGENTS.md`, `PRODUCT.md`, and `docs/agent/QUALITY_GATES.md` before changing files.
2. Inspect Git status without cleaning, stashing, resetting, or committing existing user changes.
3. Freeze a consistent validation snapshot: stop concurrent writers, record the selected-file hashes/status before the run, and restart the audit if they change before evidence capture finishes.
4. Choose one route:
   - **Change check**: run the smallest relevant gates, then the required release gate if delivery is requested.
   - **Release candidate**: read [references/release-checklist.md](references/release-checklist.md), run the full profile, build once, and verify the archive.
   - **Gate failure**: use the JSON report to separate environment failure, pre-existing failure, and current regression; reproduce the failed command directly.
   - **Rollback**: read the rollback section in the checklist and verify exact paths/hashes before proposing restoration.

## Use the shared quality contract

Start by inspecting, not guessing:

```text
<python> scripts/quality_gate.py --list
<python> scripts/quality_gate.py --profile fast --dry-run
```

Use `fast` for quick deterministic feedback and `full` for a release candidate or a change that crosses Web/MCP/data/contract boundaries. Write a machine report when handing off evidence:

```text
<python> scripts/quality_gate.py --profile full --report-json artifacts/quality-release.json
```

Treat `failed`, `timed_out`, `missing_tool`, and an undeclared skip as failures. Do not rewrite thresholds, tests, or manifests to make a run green.

## Build a release candidate

Only after the full gate passes:

```text
<python> scripts/build_release.py build --output-dir <new-evidence-dir> --expected-version <X.Y.Z>
<python> scripts/build_release.py verify --archive <new-evidence-dir>/biodata-agent-release-candidate.zip
```

Test the unpacked archive in a fresh temporary directory outside the repository, then remove that directory. Never substitute a source-tree smoke test for archive verification, and never leave a nested project copy where governance or file-discovery checks can traverse it.

## Preserve boundaries

- Keep ordinary gates offline and free of real LLM keys, model downloads, and paid services.
- Never read or package `.env`, credentials, browser storage, local models, caches, outputs, logs, collaboration claims, or personal notes.
- Do not use `pull_request_target` to execute pull-request code.
- Do not push, publish, deploy, create cloud resources, or rotate secrets unless the user explicitly supplies that target and authority.
- A configured GitHub workflow is not evidence that GitHub ran it. A built artifact is not evidence that production was deployed.
- Stop before release if security tests, Web/MCP parity, the frozen retrieval evaluation, archive verification, or rollback evidence is incomplete.

## Report the result

Return:

1. exact root, commit and dirty-state boundary;
2. selected route and why;
3. commands, exit codes and report paths;
4. passed, failed, skipped and fallback details;
5. archive/file digests when packaging;
6. rollback location and the exact files it covers when applicable; otherwise report `N/A` and explain why no source-tree or deployment rollback is applicable;
7. remaining conditions that require remote settings or deployment authority.

If the repository changed during validation, report the evidence as an unstable snapshot and rerun from step 1; do not merge results from two states.
