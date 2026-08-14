# Full Artemis QA on Windows - exit non-zero on failure.
param(
  [switch]$SkipDocker,
  [switch]$SkipTui
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
. (Join-Path $PSScriptRoot "lib\windows-docker.ps1")

function Log($m) { Write-Host "[qa] $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "[qa] FAIL: $m" -ForegroundColor Red; exit 1 }

function Initialize-DockerCli { Initialize-ArtemisDockerCli }
function Wait-DockerReady { Wait-ArtemisDockerReady @args }

$uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
if (-not (Test-Path $uv)) { Fail "run scripts/install.ps1 first" }
$env:Path = "$(Split-Path $uv);$env:Path"
if (-not $SkipDocker) { Initialize-DockerCli }

Log "ruff..."
& $uv run ruff check backend tests scripts

Log "pytest..."
& $uv run python -m pytest tests/ -q --tb=no
if ($LASTEXITCODE -ne 0) { Fail "pytest" }

if (-not $SkipTui) {
  if (-not (Get-Command bun -ErrorAction SilentlyContinue)) { Fail "bun missing" }
  Log "TUI typecheck..."
  Push-Location chassis/packages/tui
  & bun run typecheck
  if ($LASTEXITCODE -ne 0) { Fail "typecheck" }
  Log "TUI tests..."
  & bun test --timeout 30000
  if ($LASTEXITCODE -ne 0) { Fail "tui tests" }
  Pop-Location
}

if (-not $SkipDocker) {
  if (-not (Wait-DockerReady)) { Fail "Docker not running" }
  Log "Docker pack smoke..."
  & $uv run python scripts/smoke_packs.py
  if ($LASTEXITCODE -ne 0) { Fail "smoke_packs" }
}

Log "ALL QA PASSED"
