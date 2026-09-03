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
Repair-ArtemisWindowsPath

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
  if ($null -eq $Args) { $Args = @() }
  & $Exe @Args 2>&1 | ForEach-Object {
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

Log "TUI binary (fast cold start)..."
Push-Location $RepoRoot
bash scripts/build-tui.sh
if ($LASTEXITCODE -ne 0) {
  Log "TUI binary build failed — artemis will fall back to slow bun src/index.ts"
}
Pop-Location

$dockerOk = Ensure-Docker
if ($dockerOk) {
  $artemisCache = if ($env:ARTEMIS_CACHE) { $env:ARTEMIS_CACHE } else { Join-Path $env:USERPROFILE ".cache\artemis" }
  function New-SandboxCtx {
    $src = Join-Path $RepoRoot "sandbox"
    $unique = "sandbox-{0}-{1}" -f $PID, [guid]::NewGuid().ToString("N").Substring(0, 8)
    $dest = Join-Path $artemisCache ("docker-ctx\" + $unique)
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    Copy-Item -Recurse $src $dest
    Get-ChildItem -Path $dest -Recurse -Force -Filter "._*" | Remove-Item -Force -ErrorAction SilentlyContinue
    Get-ChildItem -Path $dest -Recurse -Force -Filter ".DS_Store" | Remove-Item -Force -ErrorAction SilentlyContinue
    return $dest
  }
  $coreOk = $false
  foreach ($attempt in 1..3) {
    Log "Building ctf-sandbox-core (L0) (attempt $attempt/3)..."
    $ctxRoot = New-SandboxCtx
    try {
      Invoke-ArtemisDocker build -f (Join-Path $ctxRoot "Dockerfile.core") -t ctf-sandbox-core $ctxRoot
      if ($LASTEXITCODE -eq 0) { $coreOk = $true; break }
    } finally {
      if (Test-Path $ctxRoot) { Remove-Item -Recurse -Force $ctxRoot -ErrorAction SilentlyContinue }
    }
    Log "ctf-sandbox-core failed (attempt $attempt) — retrying in 20s"
    Start-Sleep -Seconds 20
  }
  if (-not $coreOk) { throw "docker build ctf-sandbox-core failed after 3 attempts" }
  if ($Full) {
    Log "Building all donor images..."
    $donors = @(
      @("Dockerfile.pwn", "ctf-sandbox-pwn"),
      @("Dockerfile.mobile", "ctf-sandbox-mobile"),
      @("Dockerfile.crypto", "ctf-sandbox-crypto"),
      @("Dockerfile.crypto-tools", "ctf-sandbox-crypto-tools"),
      @("Dockerfile.ghidra", "ctf-sandbox-ghidra"),
      @("Dockerfile.steg", "ctf-sandbox-steg"),
      @("Dockerfile.linux", "ctf-sandbox-linux")
    )
    foreach ($d in $donors) {
      $ok = $false
      foreach ($attempt in 1..3) {
        Log "docker build $($d[1]) (attempt $attempt/3)..."
        $ctxRoot = New-SandboxCtx
        try {
          Invoke-ArtemisDocker build -f (Join-Path $ctxRoot $d[0]) -t $d[1] $ctxRoot
          if ($LASTEXITCODE -eq 0) { $ok = $true; break }
        } finally {
          if (Test-Path $ctxRoot) { Remove-Item -Recurse -Force $ctxRoot -ErrorAction SilentlyContinue }
        }
        Log "$($d[1]) failed (attempt $attempt) — retrying in 20s"
        Start-Sleep -Seconds 20
      }
      if (-not $ok) { throw "docker build $($d[1]) failed after 3 attempts" }
    }
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
$ps = Join-Path $PSHOME "powershell.exe"
if (-not (Test-Path -LiteralPath $ps)) { $ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe" }
& $ps -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "scripts\verify-install.ps1") @verifyArgs

Write-Host ""
Write-Host "Artemis install complete." -ForegroundColor Green
Write-Host "  Open a NEW terminal, then run:  artemis"
Write-Host "  Headless:     artemis swarm --challenge PATH --models cursor/composer-2.5 -v"
Write-Host "  Connect keys: open TUI then /connect"
Write-Host ""
