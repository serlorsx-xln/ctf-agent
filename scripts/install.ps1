# Artemis one-shot installer (Windows native / PowerShell 5+).
# Usage: powershell -ExecutionPolicy Bypass -File scripts/install.ps1 [-Full] [-SkipDocker] [-SkipBake]
param(
  [switch]$Full,
  [switch]$SkipDocker,
  [switch]$SkipBake
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
. (Join-Path $PSScriptRoot "lib\windows-docker.ps1")
. (Join-Path $PSScriptRoot "lib\windows-path.ps1")

$env:PYTHONIOENCODING = "utf-8"

function Log($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# Run native CLI; capture exit code without mixing stdout into the return value (PS5).
function Invoke-External {
  param(
    [Parameter(Mandatory)][string]$Exe,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Args
  )
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  $quoted = $Args | ForEach-Object {
    if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
  }
  $cmd = if ($quoted.Count -gt 0) { "$Exe $($quoted -join ' ')" } else { $Exe }
  cmd /c $cmd 2>&1 | ForEach-Object {
    if ($_ -is [System.Management.Automation.ErrorRecord]) { Write-Host $_.ToString() }
    else { Write-Host $_ }
  }
  $script:InvokeExternalExitCode = $LASTEXITCODE
  $ErrorActionPreference = $prev
}

function Ensure-Uv {
  $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
  if (Test-Path $uv) { Log "uv $(& $uv --version)"; return $uv }
  Log "Installing uv..."
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  irm https://astral.sh/uv/install.ps1 | iex
  $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
  if (-not (Test-Path $uv)) { throw "uv install failed" }
  return $uv
}

function Ensure-Bun {
  if (Get-Command bun -ErrorAction SilentlyContinue) { Log "bun $(bun --version)"; return }
  Log "Installing Bun..."
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  irm https://bun.sh/install.ps1 | iex
  $env:Path = "$env:USERPROFILE\.bun\bin;$env:Path"
  if (-not (Get-Command bun -ErrorAction SilentlyContinue)) { throw "Bun install failed" }
}

function Sync-PythonDeps {
  param([Parameter(Mandatory)][string]$Uv)
  if (-not $env:UV_LINK_MODE) { $env:UV_LINK_MODE = "copy" }
  & $Uv sync --python 3.14
  & $Uv run python -c "from pydantic_ai.usage import RunUsage" 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Log "Repairing incomplete pydantic-ai install..."
    & $Uv sync --reinstall-package pydantic-ai-slim --reinstall-package pydantic-ai --python 3.14
    & $Uv run python -c "from pydantic_ai.usage import RunUsage" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "pydantic-ai install failed" }
  }
}

function Ensure-Docker {
  if ($SkipDocker) { Log "Skipping Docker (-SkipDocker)"; return $false }
  Initialize-ArtemisDockerCli
  if (Test-ArtemisDockerDaemon) {
    Log "Docker OK"
    return $true
  }
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Log "Docker CLI not found - installing Docker Desktop..."
    if (-not (Install-ArtemisDockerDesktop)) { return $false }
  } else {
    Log "Waiting for Docker daemon..."
  }
  if (Wait-ArtemisDockerReady -MaxWaitSeconds 120) {
    Log "Docker OK"
    return $true
  }
  Log "Docker not ready - ensure Docker Desktop is running, then: powershell -File scripts\install.ps1"
  return $false
}

Log "Artemis install at $RepoRoot"
$uv = Ensure-Uv
$env:Path = "$(Split-Path $uv);$env:USERPROFILE\.bun\bin;$env:Path"
Ensure-Bun

Log "Python deps (uv sync, Python 3.14)..."
Sync-PythonDeps -Uv $uv

Log "TUI deps (bun install in chassis/)..."
Log "Skipping native node-gyp scripts (no Visual Studio required on Windows)"
Push-Location chassis
Invoke-External bun install --ignore-scripts
$bi = $script:InvokeExternalExitCode
if ($bi -ne 0) { throw "bun install failed (exit $bi)" }
Pop-Location

$dockerOk = Ensure-Docker
if ($dockerOk) {
  Log "Building ctf-sandbox-core (L0)..."
  Invoke-ArtemisDocker build -f sandbox/Dockerfile.core -t ctf-sandbox-core .
  if ($LASTEXITCODE -ne 0) { throw "docker build ctf-sandbox-core failed (exit $LASTEXITCODE)" }
  if ($Full) {
    Log "Building donor images..."
    Invoke-ArtemisDocker build -f sandbox/Dockerfile.pwn -t ctf-sandbox-pwn .
    Invoke-ArtemisDocker build -f sandbox/Dockerfile.mobile -t ctf-sandbox-mobile .
    Invoke-ArtemisDocker build -f sandbox/Dockerfile.crypto -t ctf-sandbox-crypto .
    Invoke-ArtemisDocker build -f sandbox/Dockerfile.ghidra -t ctf-sandbox-ghidra .
    Invoke-ArtemisDocker build -f sandbox/Dockerfile.steg -t ctf-sandbox-steg .
    Invoke-ArtemisDocker build -f sandbox/Dockerfile.linux -t ctf-sandbox-linux .
    if (-not $SkipBake) {
      Log "Warming pack caches..."
      & $uv run artemis setup -v
    }
  }
}

Install-ArtemisCli -RepoRoot $RepoRoot

Log "Running verify-install..."
$verifyArgs = @()
if ($SkipDocker -or -not $dockerOk) { $verifyArgs += "-SkipDocker" }
& powershell -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "scripts\verify-install.ps1") @verifyArgs

Write-Host ""
Write-Host "Artemis install complete." -ForegroundColor Green
Write-Host "  Open a NEW terminal, then run:  artemis"
Write-Host "  Headless:     artemis swarm --challenge PATH --models cursor/composer-2.5 -v"
Write-Host "  Connect keys: open TUI then /connect"
Write-Host ""
