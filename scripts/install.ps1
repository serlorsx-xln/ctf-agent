# Artemis one-shot installer (Windows native / PowerShell 5+).
# Usage: powershell -ExecutionPolicy Bypass -File scripts/install.ps1 [-Full] [-SkipDocker]
param(
  [switch]$Full,
  [switch]$SkipDocker,
  [switch]$SkipBake
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Log($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

function Ensure-Uv {
  $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
  if (Test-Path $uv) { Log "uv $(& $uv --version)"; return $uv }
  Log "Installing uv..."
  irm https://astral.sh/uv/install.ps1 | iex
  $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
  if (-not (Test-Path $uv)) { throw "uv install failed" }
  return $uv
}

function Ensure-Bun {
  if (Get-Command bun -ErrorAction SilentlyContinue) { Log "bun $(bun --version)"; return }
  Log "Installing Bun..."
  irm https://bun.sh/install.ps1 | iex
  $env:Path = "$env:USERPROFILE\.bun\bin;$env:Path"
  if (-not (Get-Command bun -ErrorAction SilentlyContinue)) { throw "Bun install failed" }
}

function Ensure-Docker {
  if ($SkipDocker) { Log "Skipping Docker (-SkipDocker)"; return $false }
  if (-not $env:DOCKER_HOST) { $env:DOCKER_HOST = "npipe:////./pipe/docker_engine" }
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Log "Docker CLI not found - installing Docker Desktop via winget..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
      winget install Docker.DockerDesktop --accept-package-agreements --accept-source-agreements --silent 2>$null
      Log "Docker Desktop installed (or already present). Start it, then re-run install.ps1 -Full"
    } else {
      Log "Install Docker Desktop: https://docs.docker.com/desktop/setup/install/windows-install/"
    }
    return $false
  }
  docker info 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {
    Log "Docker OK"
    return $true
  }
  Log "Docker installed but not running - start Docker Desktop from the Start menu"
  return $false
}

Log "Artemis install at $RepoRoot"
$uv = Ensure-Uv
$env:Path = "$(Split-Path $uv);$env:Path"
Ensure-Bun

Log "Python deps (uv sync, Python 3.14)..."
& $uv sync --python 3.14

Log "TUI deps (bun install in chassis/)..."
Push-Location chassis
& bun install 2>$null
if ($LASTEXITCODE -ne 0) {
  Log "bun install: optional native modules failed - retrying with --ignore-scripts"
  & bun install --ignore-scripts
}
Pop-Location

$dockerOk = Ensure-Docker
if ($dockerOk) {
  Log "Building ctf-sandbox-core (L0)..."
  docker build -f sandbox/Dockerfile.core -t ctf-sandbox-core .
  if ($Full) {
    Log "Building donor images..."
    docker build -f sandbox/Dockerfile.pwn -t ctf-sandbox-pwn .
    docker build -f sandbox/Dockerfile.mobile -t ctf-sandbox-mobile .
    docker build -f sandbox/Dockerfile.crypto -t ctf-sandbox-crypto .
    docker build -f sandbox/Dockerfile.ghidra -t ctf-sandbox-ghidra .
    docker build -f sandbox/Dockerfile.steg -t ctf-sandbox-steg .
    docker build -f sandbox/Dockerfile.linux -t ctf-sandbox-linux .
    if (-not $SkipBake) {
      Log "Warming pack caches..."
      & $uv run artemis setup -v
    }
  }
}

Write-Host ""
Write-Host "Artemis install complete." -ForegroundColor Green
Write-Host "  Launch TUI:   chassis\bin\artemis.cmd"
Write-Host "  Headless:     uv run artemis swarm --challenge PATH --models cursor/composer-2.5 -v"
Write-Host "  QA smoke:     powershell -File scripts\qa.ps1"
Write-Host "  Connect keys: open TUI then /connect"
Write-Host ""
