[CmdletBinding()]
param(
    [string] $Root = '',
    [int] $MaxProjectInstructionBytes = 32768
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($Root)) {
    $ScriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
    $Root = Split-Path -Parent $ScriptDirectory
}

$Failures = New-Object 'System.Collections.Generic.List[string]'
$Warnings = New-Object 'System.Collections.Generic.List[string]'

function Add-Failure([string] $Message) {
    $Failures.Add($Message)
}

function Add-Warning([string] $Message) {
    $Warnings.Add($Message)
}

function Get-RelativeName([string] $Path, [string] $BasePath) {
    $FullPath = [IO.Path]::GetFullPath($Path)
    if ($FullPath.StartsWith($BasePath, [StringComparison]::OrdinalIgnoreCase)) {
        return $FullPath.Substring($BasePath.Length).TrimStart([char[]]@('\', '/'))
    }
    return $FullPath
}

function Get-ProjectInstructionFiles([string] $RootPath, [string[]] $Names) {
    $SkipDirectoryNames = @(
        '.git', '.hg', '.svn', '.cache', '.pytest_cache', '.mypy_cache', '.ruff_cache',
        '.tox', '.nox', 'node_modules', 'vendor', 'dist', 'build', 'target', '__pycache__'
    )
    $Pending = New-Object 'System.Collections.Generic.Stack[string]'
    $Pending.Push($RootPath)

    while ($Pending.Count -gt 0) {
        $Current = $Pending.Pop()
        Get-ChildItem -LiteralPath $Current -Force -File -ErrorAction SilentlyContinue |
            Where-Object { $Names -contains $_.Name } |
            ForEach-Object { $_ }

        Get-ChildItem -LiteralPath $Current -Force -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $Skip = $SkipDirectoryNames -contains $_.Name
            if ($_.Name -like '.venv*' -or $_.Name -like 'venv*') { $Skip = $true }
            if (-not $Skip) { $Pending.Push($_.FullName) }
        }
    }
}

$ResolvedRoot = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Root).Path).TrimEnd([char[]]@('\', '/'))
$RootAgents = Join-Path $ResolvedRoot 'AGENTS.md'
$RootClaude = Join-Path $ResolvedRoot 'CLAUDE.md'

$RequiredFiles = @(
    'AGENTS.md',
    'CLAUDE.md',
    'MODULES.md',
    'docs\agent\PROJECT_CONTEXT.md',
    'docs\agent\ARCHITECTURE_AND_CONTRACTS.md',
    'docs\agent\COLLABORATION.md',
    'docs\agent\LOGGING.md',
    'docs\agent\QUALITY_GATES.md',
    'docs\agent\WINDOWS_ENVIRONMENT.md',
    '协同\认领\_模板.md',
    '协同\交接\_模板.md',
    'scripts\validate-agent-governance.ps1'
)

foreach ($RelativePath in $RequiredFiles) {
    $FullPath = Join-Path $ResolvedRoot $RelativePath
    if (-not (Test-Path -LiteralPath $FullPath -PathType Leaf)) {
        Add-Failure "Missing required file: $RelativePath"
    }
}

$InstructionNames = @('AGENTS.md', 'AGENTS.override.md', 'CLAUDE.md', 'CLAUDE.local.md')
$InstructionFiles = @(Get-ProjectInstructionFiles -RootPath $ResolvedRoot -Names $InstructionNames)

foreach ($File in $InstructionFiles) {
    $RelativeName = Get-RelativeName -Path $File.FullName -BasePath $ResolvedRoot
    if ($File.Name -eq 'AGENTS.override.md') {
        Add-Failure "Project AGENTS.override.md is forbidden because it shadows AGENTS.md: $RelativeName"
        continue
    }
    if ($File.Name -eq 'AGENTS.md' -and -not [string]::Equals($File.FullName, $RootAgents, [StringComparison]::OrdinalIgnoreCase)) {
        Add-Failure "Unexpected nested AGENTS.md: $RelativeName"
        continue
    }
    if ($File.Name -eq 'CLAUDE.md' -and -not [string]::Equals($File.FullName, $RootClaude, [StringComparison]::OrdinalIgnoreCase)) {
        Add-Failure "Unexpected nested CLAUDE.md: $RelativeName"
        continue
    }
    if ($File.Name -eq 'CLAUDE.local.md') {
        Add-Warning "CLAUDE.local.md found; use /memory to confirm it does not conflict with project policy: $RelativeName"
    }
}

$ClaudeRules = Join-Path $ResolvedRoot '.claude\rules'
if (Test-Path -LiteralPath $ClaudeRules -PathType Container) {
    Get-ChildItem -LiteralPath $ClaudeRules -Recurse -File -Force | ForEach-Object {
        Add-Failure "Unexpected .claude/rules file: $(Get-RelativeName -Path $_.FullName -BasePath $ResolvedRoot)"
    }
}

if (Test-Path -LiteralPath $RootAgents -PathType Leaf) {
    $AgentsBytes = (Get-Item -LiteralPath $RootAgents).Length
    if ($AgentsBytes -gt $MaxProjectInstructionBytes) {
        Add-Failure "Root AGENTS.md is $AgentsBytes bytes; project instruction budget is $MaxProjectInstructionBytes bytes."
    }

    $AgentsText = Get-Content -LiteralPath $RootAgents -Raw -Encoding UTF8
    $RequiredSnippets = @(
        'docs/agent/COLLABORATION.md',
        'docs/agent/LOGGING.md',
        'writer / worktree',
        'git status --short',
        'scripts/logrotate.py',
        'QUALITY_GATES.md',
        'contract_change'
    )
    foreach ($Snippet in $RequiredSnippets) {
        if (-not $AgentsText.Contains($Snippet)) {
            Add-Failure "AGENTS.md is missing required governance marker: $Snippet"
        }
    }
}

if (Test-Path -LiteralPath $RootClaude -PathType Leaf) {
    $ClaudeText = Get-Content -LiteralPath $RootClaude -Raw -Encoding UTF8
    $ImportCount = ([regex]::Matches($ClaudeText, '(?m)^\s*@AGENTS\.md\s*$')).Count
    if ($ImportCount -ne 1) {
        Add-Failure "CLAUDE.md must contain exactly one standalone @AGENTS.md import; found $ImportCount."
    }
    $ClaudeLineCount = (Get-Content -LiteralPath $RootClaude -Encoding UTF8).Count
    if ($ClaudeLineCount -gt 200) {
        Add-Failure "CLAUDE.md has $ClaudeLineCount lines; the recommended maximum is 200."
    }
}

$TemplateChecks = @{
    '协同\认领\_模板.md' = @(
        'schema_version', 'claim_id', 'created_at', 'updated_at', 'agent_instance', 'coordinator',
        'ACTIVE', 'VERIFYING', 'READY_TO_MERGE', 'BLOCKED', 'branch', 'base_ref', 'worktree', 'contract_change'
    )
    '协同\交接\_模板.md' = @(
        'schema_version', 'handoff_id', 'created_at', 'updated_at', 'OPEN', 'ACCEPTED', 'IN_PROGRESS',
        'VERIFYING', 'BLOCKED', 'created_by', 'assigned_to', 'source_branch', 'source_claim', 'compatibility', 'blocking'
    )
}

foreach ($RelativePath in $TemplateChecks.Keys) {
    $TemplatePath = Join-Path $ResolvedRoot $RelativePath
    if (-not (Test-Path -LiteralPath $TemplatePath -PathType Leaf)) { continue }
    $TemplateText = Get-Content -LiteralPath $TemplatePath -Raw -Encoding UTF8
    foreach ($Marker in $TemplateChecks[$RelativePath]) {
        if (-not $TemplateText.Contains($Marker)) {
            Add-Failure "Template $RelativePath is missing required marker: $Marker"
        }
    }

    $OpenPlaceholders = ([regex]::Matches($TemplateText, '\{\{')).Count
    $ClosePlaceholders = ([regex]::Matches($TemplateText, '\}\}')).Count
    if ($OpenPlaceholders -ne $ClosePlaceholders) {
        Add-Failure "Template $RelativePath has unbalanced placeholder braces: $OpenPlaceholders open / $ClosePlaceholders close."
    }
    $WithoutComments = [regex]::Replace($TemplateText, '(?s)<!--.*?-->', '')
    if ($WithoutComments -match '<[A-Za-z][^>\r\n]*>') {
        Add-Failure "Template $RelativePath contains an HTML-like angle-bracket placeholder."
    }
}

$CollaborationPath = Join-Path $ResolvedRoot 'docs\agent\COLLABORATION.md'
if (Test-Path -LiteralPath $CollaborationPath -PathType Leaf) {
    $CollaborationText = Get-Content -LiteralPath $CollaborationPath -Raw -Encoding UTF8
    $TransitionPatterns = @(
        '(?s)ACTIVE.*VERIFYING.*READY_TO_MERGE.*MERGED',
        '(?s)OPEN.*ACCEPTED.*IN_PROGRESS.*VERIFYING.*CLOSED',
        'contract_change: none \| compatible \| breaking'
    )
    foreach ($Pattern in $TransitionPatterns) {
        if ($CollaborationText -notmatch $Pattern) {
            Add-Failure "Collaboration state model is missing expected transition pattern: $Pattern"
        }
    }
}

$Utf8Strict = New-Object System.Text.UTF8Encoding($false, $true)
$MarkdownFiles = @()
foreach ($RelativePath in ($RequiredFiles | Where-Object { [IO.Path]::GetExtension($_) -eq '.md' })) {
    $FullPath = Join-Path $ResolvedRoot $RelativePath
    if (Test-Path -LiteralPath $FullPath -PathType Leaf) {
        $MarkdownFiles += Get-Item -LiteralPath $FullPath
    }
}
$RelativeLinkCount = 0
$PowerShellBlockCount = 0

foreach ($File in $MarkdownFiles) {
    $RelativeName = Get-RelativeName -Path $File.FullName -BasePath $ResolvedRoot
    $Bytes = [IO.File]::ReadAllBytes($File.FullName)
    if ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xEF -and $Bytes[1] -eq 0xBB -and $Bytes[2] -eq 0xBF) {
        Add-Failure "Markdown must be UTF-8 without BOM: $RelativeName"
    }

    try {
        $Text = $Utf8Strict.GetString($Bytes)
    }
    catch {
        Add-Failure "Markdown is not valid UTF-8: $RelativeName"
        continue
    }

    if ($Text.Contains("`r`n")) {
        Add-Failure "Markdown must use LF line endings: $RelativeName"
    }

    # CRLF 已记为失败；此处再在内存里归一 LF，令下方 fence/link/PowerShell 块正则对 \r 免疫
    # （否则 CRLF 会破坏 ``` 代码围栏剥离，把块内 [string]($Var) 之类误判成断链，产生连锁误报）。
    $Text = $Text -replace "`r`n", "`n"

    $FenceCount = ([regex]::Matches($Text, '(?m)^[ \t]*```')).Count
    if (($FenceCount % 2) -ne 0) {
        Add-Failure "Odd number of Markdown code fences: $RelativeName ($FenceCount)"
    }

    $PowerShellBlocks = [regex]::Matches($Text, '(?ms)^[ \t]*```powershell[ \t]*\r?\n(.*?)^[ \t]*```[ \t]*$')
    foreach ($Block in $PowerShellBlocks) {
        $PowerShellBlockCount++
        $Tokens = $null
        $ParseErrors = $null
        [void][System.Management.Automation.Language.Parser]::ParseInput($Block.Groups[1].Value, [ref] $Tokens, [ref] $ParseErrors)
        foreach ($ParseError in $ParseErrors) {
            Add-Failure "PowerShell block parse error in ${RelativeName}: $($ParseError.Message)"
        }
    }

    $LinkText = [regex]::Replace($Text, '(?ms)^[ \t]*```.*?^[ \t]*```[ \t]*$', '')
    $LinkText = [regex]::Replace($LinkText, '`[^`\r\n]*`', '')
    $Links = [regex]::Matches($LinkText, '(?<!!)\[[^\]]+\]\(([^)]+)\)')
    foreach ($Link in $Links) {
        $Target = (($Link.Groups[1].Value.Trim() -split '\s+', 2)[0]).Trim([char[]]'<>')
        if ($Target -match '^[A-Za-z][A-Za-z0-9+.-]*:' -or $Target.StartsWith('#')) { continue }

        $RelativeLinkCount++
        $PathPart = ($Target -split '#', 2)[0]
        $PathPart = [Uri]::UnescapeDataString($PathPart)
        $ResolvedTarget = [IO.Path]::GetFullPath((Join-Path $File.DirectoryName $PathPart))
        if (-not (Test-Path -LiteralPath $ResolvedTarget)) {
            Add-Failure "Relative link target does not exist: $RelativeName -> $Target"
        }
    }
}

foreach ($Message in $Warnings) {
    Write-Warning $Message
}

if ($Failures.Count -gt 0) {
    Write-Host "AGENT GOVERNANCE CHECK FAILED ($($Failures.Count))" -ForegroundColor Red
    foreach ($Message in $Failures) {
        Write-Host "- $Message" -ForegroundColor Red
    }
    exit 1
}

Write-Host 'AGENT GOVERNANCE CHECK PASSED' -ForegroundColor Green
Write-Host "Root: $ResolvedRoot"
Write-Host "Markdown files: $($MarkdownFiles.Count)"
Write-Host "Relative links checked: $RelativeLinkCount"
Write-Host "PowerShell blocks parsed: $PowerShellBlockCount"
Write-Host "Project instruction budget: $((Get-Item -LiteralPath $RootAgents).Length) / $MaxProjectInstructionBytes bytes"
exit 0
