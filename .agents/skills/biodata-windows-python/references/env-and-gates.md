# Environment and gates (BioData Agent)

Full probe script: `docs/agent/WINDOWS_ENVIRONMENT.md` section 2. Full matrix: `docs/agent/QUALITY_GATES.md` section 4.

## Interpreter probe (condensed)

```powershell
$ErrorActionPreference = 'Stop'
$RepoRoot = (git rev-parse --show-toplevel | Select-Object -Last 1).Trim()
$Python = $null
foreach ($cand in @($env:BIODATA_PYTHON, (Join-Path $RepoRoot '.venv\Scripts\python.exe'))) {
  if ($cand -and (Test-Path -LiteralPath $cand)) { $Python = $cand; break }
}
if (-not $Python) {
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) { $Python = "$($py.Source) -3" }
  else {
    foreach ($n in 'python3','python') {
      $c = Get-Command $n -ErrorAction SilentlyContinue
      if ($c) { $Python = $c.Source; break }
    }
  }
}
if (-not $Python) { throw 'No Python 3.10+ found; stop and report.' }
```

Confirm the interpreter and its dependencies before trusting it:

```powershell
& $Python -c "import sys; assert sys.version_info >= (3,10); print(sys.executable)"
& $Python -c "import pytest; print(pytest.__version__)"
```

## Gate matrix by change type

| Change | Minimum verification |
|---|---|
| Docs only | links, filenames, UTF-8, commands, fact snapshots |
| Single backend function | targeted pytest plus affected-module tests |
| Query parse / retrieval / ranking / workflow / base data | targeted tests plus full pytest plus frozen evaluation |
| External load or upload | external/upload tests plus base-767 guard plus frozen evaluation |
| Frontend JS/CSS/HTML | Web smoke plus real-browser check for visual or interaction changes |
| HTTP response field shape or name | backend tests plus Web smoke plus update every consumer per the MODULES.md map (`$biodata-frontend-contract`) |
| MCP server | MCP unit tests plus real stdio `mcp_server.py --selfcheck` plus an error path |
| CLI | args, exit codes, stdout/stderr, machine-readable output |
| Public schema or contract | producer plus all consumers jointly, with a migration note |
| Log / claim / handoff mechanism | governance script plus `logrotate.py` plus template and state tests |

## Do not

- Do not claim CI ran because a `.github/workflows/*.yml` file exists; a config is not a run.
- Do not install dependencies, download models, or reach the network inside an ordinary gate.
