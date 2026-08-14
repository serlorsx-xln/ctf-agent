# Post-install checks - Windows. Exit 0 when ready (Docker optional with -SkipDocker).
param(
  [switch]$SkipDocker,
  [switch]$StructureOnly
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$ok = 0
$fail = 0
function Check([scriptblock]$Test, [string]$Name) {
  if (& $Test) {
    Write-Host "  OK   $Name" -ForegroundColor Green
    $script:ok++
  } else {
    Write-Host "  FAIL $Name" -ForegroundColor Red
    $script:fail++
  }
}

Write-Host "Artemis verify @ $RepoRoot"

Check { Test-Path pyproject.toml } "pyproject.toml"
Check { Test-Path sandbox/Dockerfile.core } "Dockerfile.core"
Check { Test-Path chassis/bin/artemis.cmd } "artemis.cmd"
Check { Test-Path scripts/install.ps1 } "install.ps1"
Check { Test-Path backend/platform_paths.py } "platform_paths.py"

if ($StructureOnly) {
  Write-Host "Structure: $ok ok, $fail fail"
  if ($fail -gt 0) { exit 1 }
  exit 0
}

$localBin = Join-Path $env:USERPROFILE ".local\bin"
$uv = Join-Path $localBin "uv.exe"
$env:Path = "$localBin;$env:USERPROFILE\.bun\bin;$env:Path"
Check { Test-Path $uv } "uv.exe"
Check { Get-Command bun -ErrorAction SilentlyContinue } "bun"
Check { Test-Path (Join-Path $RepoRoot ".venv") } ".venv"

if (Test-Path $uv) {
  Check {
    & $uv run python -c 'import backend.platform_paths' 2>$null | Out-Null
    $LASTEXITCODE -eq 0
  } "import backend"
  Check {
    & $uv run python -c 'from pydantic_ai.usage import RunUsage' 2>$null | Out-Null
    $LASTEXITCODE -eq 0
  } "pydantic-ai"
}

Check { Test-Path (Join-Path $RepoRoot "chassis/node_modules") } "chassis node_modules"
Check {
  (Test-Path (Join-Path $RepoRoot "chassis/node_modules/tree-sitter-powershell")) -or
    (Test-Path (Join-Path $RepoRoot "chassis/node_modules/.bun/tree-sitter-powershell@0.25.10"))
} "tree-sitter-powershell (no VS build required)"
Check {
  Test-Path (Join-Path $RepoRoot "chassis/packages/opencode/src/index.ts")
} "opencode TUI entry"

$artemisCmd = Join-Path $localBin "artemis.cmd"
Check { Test-Path $artemisCmd } "global artemis.cmd"
Check {
  $parts = ([Environment]::GetEnvironmentVariable("Path", "User") -split ';' | ForEach-Object { $_.Trim() })
  $parts -contains $localBin
} "user PATH includes .local/bin"

if (Test-Path $uv) {
  Check {
    & $uv run --directory $RepoRoot artemis --help 2>$null | Out-Null
    $LASTEXITCODE -eq 0
  } "artemis CLI (--help)"
}

if (-not $SkipDocker) {
  . (Join-Path $PSScriptRoot "lib\windows-docker.ps1")
  Initialize-ArtemisDockerCli
  if (Test-ArtemisDockerDaemon) {
    Check {
      $prev = $ErrorActionPreference
      $ErrorActionPreference = "Continue"
      cmd /c "docker image inspect ctf-sandbox-core >nul 2>&1"
      $ok = ($LASTEXITCODE -eq 0)
      $ErrorActionPreference = $prev
      $ok
    } "ctf-sandbox-core image"
  } else {
    Write-Host "  WARN Docker not running" -ForegroundColor Yellow
    $fail++
  }
}

Write-Host "Result: $ok ok, $fail fail"
if ($fail -gt 0) { exit 1 }
