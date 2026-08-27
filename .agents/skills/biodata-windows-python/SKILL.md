---
name: biodata-windows-python
description: Resolve a real Python 3.10+ interpreter on this Windows workstation and run the BioData Agent quality gate, pytest, frozen evaluation, and git correctly, including why the tooling uses PowerShell rather than Bash on this non-ASCII repository path. Use before running any Python, pytest, or git command in this repository.
---

# BioData Windows and Python

Deterministic, offline, evidence-first command execution. Authoritative sources: `docs/agent/WINDOWS_ENVIRONMENT.md` and `docs/agent/QUALITY_GATES.md`. Full interpreter probe and per-risk gate matrix: [references/env-and-gates.md](references/env-and-gates.md).

## Resolve `$Python`; never assume `py`, `python`, or `python3` exist

Resolution order: `$env:BIODATA_PYTHON` hint, then project `.venv\Scripts\python.exe`, then `py -3`, then `python3` or `python`, then (opt-in only) `uv python find`. Verify `sys.version_info >= (3, 10)` and record the actual `sys.executable`. `$RepoRoot` is the git top-level, not the current directory.

## Use PowerShell for git and Python, not the Bash tool

This repository path contains non-ASCII (CJK) characters. The Bash tool can hang on it, so run git, Python, pytest, and file operations through PowerShell with quoted `-LiteralPath`. For stdio MCP, stdout carries protocol only; send diagnostics to stderr.

## File conventions

UTF-8 without BOM, LF line endings. In JSON, write backslashes as `\\`. Read CJK Markdown as explicit UTF-8.

## Run the gates (single source of truth is `automation/quality-gates.json`)

- `& $Python scripts\quality_gate.py --list`, then `--profile fast|full`. `fast` is Python AST compile plus browser JS syntax plus automation, CI, release, and skill contract tests. `full` is fast plus `pip check`, agent governance, full pytest, project, Web, and MCP smoke, and the frozen recommendation evaluation. A delivery candidate must run `full`.
- Frozen evaluation: `& $Python scripts\evaluate_recommendation.py`. Baseline: base 767, Top1 97.6, Top5 97.6, hard 0, FASTQ 0, NoResult 13/13. A non-zero exit is failure; never weaken thresholds to go green.
- `& $Python -m pytest tests\ -q`; `& $Python scripts\web_smoke_test.py` (expects `WEB SMOKE TEST PASSED`); `& $Python scripts\logrotate.py` (log, claim, and handoff health).

## Read-only tasks

Set `$env:PYTHONDONTWRITEBYTECODE = '1'`, use `pytest -p no:cacheprovider`, and write no venv, temp, or report files inside the repo.

## Failure semantics

`CommandNotFoundException` means the command did not run, not that a test passed or failed. Record the real `sys.executable`. If a documented command conflicts with the current CLI `--help`, trust the CLI and report the drift.
