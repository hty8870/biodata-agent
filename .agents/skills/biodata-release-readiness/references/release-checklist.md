# Release checklist

## Before validation

- Confirm the authoritative repository root and launcher target.
- Record `git rev-parse HEAD`, branch, remote count, and `git status --short`.
- Preserve unrelated tracked and untracked work.
- Identify the exact changed-file set and all Web/MCP/CLI consumers of a shared contract.
- Confirm no real `.env`, key, token, browser setting, model, cache, output, or private data is in scope.
- Stop concurrent writers for the validation window. Record selected-file hashes and status before validation; compare them again before declaring the result.

## Quality gates

1. Run `scripts/quality_gate.py --list` and check that each expected gate is declared.
2. Run fast gates while iterating.
3. Run full gates before packaging.
4. Require a JSON report; preserve the command, exit code and shortest relevant failure output.
5. If HTTP contracts changed, inspect every frontend consumer in addition to Web smoke.
6. If MCP changed, run real stdio `mcp_server.py --selfcheck`; an import-only test is insufficient.
7. If query parsing, retrieval, ranking or data changed, run the frozen recommendation evaluation without weakening its thresholds.

## Release candidate

- Install from `requirements-ci.lock` with `--require-hashes --only-binary=:all:` in a clean environment.
- Build with `scripts/build_release.py`; do not hand-select files in a second workflow.
- Pass `--expected-version` for versioned delivery and use a new evidence directory; the builder must not overwrite an existing candidate pair by default.
- Within packaged data inputs, include only the reviewed base corpus, five public external snapshots and frozen eval inputs; the archive also contains the allowlisted runtime, tests and public docs. Runtime `upload_...` data must stay out.
- Confirm the archive excludes `.env`, `.git`, models, virtual environments, caches, outputs, logs, collaboration notes, personal work material and user-specific absolute home paths.
- Verify `product_version`, `release-manifest.json`, every file SHA-256, the content digest and the archive sidecar digest.
- Extract to a new temporary directory outside the repository and run core, Web and MCP smoke tests there. Clean it afterward so recursive governance checks cannot discover a nested project copy.
- Keep the built archive; do not rebuild the same version during deployment or rollback.

## GitHub controls

- Use `pull_request`, `push` or explicit manual/tag triggers; never run PR code under `pull_request_target` privileges.
- Pin every Action to a full 40-character SHA and retain the release tag as a comment.
- Start from `permissions: {}` and grant only the job permissions required.
- Disable checkout credential persistence when no authenticated Git operation follows.
- Set concurrency and explicit job timeouts.
- Keep stable aggregate gate names for future Rulesets.
- Keep deployment credentials out of build jobs; use protected environments and OIDC only after a deployment target exists.

## Rollback evidence

- Store backups outside the project tree.
- Record each target relative path, its pre-sync SHA-256, and whether it was EXISTING or NEW.
- Back up only existing target files while preserving relative paths.
- Make restoration dry-run by default; require an explicit apply switch.
- Reject absolute paths, `..`, path separators in managed names, links/junctions and any resolved path outside the approved root.
- On rollback, restore EXISTING files and delete only paths explicitly marked NEW.
- Re-run health, Web/MCP and hash checks after restoration.

## Stop rules

Stop and report instead of claiming readiness when:

- files or Git status change during the validation window; label earlier output an unstable snapshot and rerun;
- a required gate did not execute or its tool is missing;
- a security rejection still reaches a network function;
- server credentials can combine with an untrusted endpoint;
- the archive differs from its manifest or contains an excluded path;
- the unpacked artifact cannot pass smoke tests;
- remote CI, Ruleset, environment, secrets or production target are missing for the claim being requested;
- rollback paths cannot be proven to stay inside the named target root.
