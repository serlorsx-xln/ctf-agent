# Artemis TUI launcher for Windows (PowerShell).
$ErrorActionPreference = "Stop"
$ChassisRoot = Split-Path -Parent $PSScriptRoot
$RepoRoot = Split-Path -Parent $ChassisRoot
$env:ARTEMIS = "1"
$env:ARTEMIS_CHASSIS_ROOT = $ChassisRoot
$env:ARTEMIS_REPO_ROOT = $RepoRoot
if (-not $env:OPENCODE_CONFIG) { $env:OPENCODE_CONFIG = Join-Path $ChassisRoot "opencode.json" }
if (-not $env:DOCKER_HOST) { $env:DOCKER_HOST = "npipe:////./pipe/docker_engine" }

function Get-PythonCmd {
  $venvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
  if (Test-Path $venvPy) { return @($venvPy) }
  return @("uv", "run", "--directory", $RepoRoot, "python")
}

if (-not (Get-Command bun -ErrorAction SilentlyContinue)) {
  Write-Error "Artemis TUI requires Bun (https://bun.sh)"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Error "Artemis requires uv (https://docs.astral.sh/uv/)"
}

$py = Get-PythonCmd
& $py @("-c", @"
from backend.shell.credentials import read_tui_api_keys
import shlex
for k, v in read_tui_api_keys().items():
    print(f'{k}={shlex.quote(v)}')
"@) 2>$null | ForEach-Object {
  if ($_ -match '^([^=]+)=(.*)$') { Set-Item -Path "env:$($Matches[1])" -Value $Matches[2] }
}

$alive = & $py @("-c", "from backend.daemon.transport import daemon_alive; import sys; sys.exit(0 if daemon_alive() else 1)")
if ($LASTEXITCODE -ne 0) {
  Start-Process -FilePath $py[0] -ArgumentList (@($py[1..($py.Length-1)] + @("-m", "backend.daemon.server"))) -WindowStyle Hidden -RedirectStandardOutput "$env:TEMP\artemis-daemon.log" -RedirectStandardError "$env:TEMP\artemis-daemon.log"
}

try {
  Invoke-WebRequest -Uri "http://127.0.0.1:18765/v1/models" -TimeoutSec 1 -UseBasicParsing | Out-Null
} catch {
  Start-Process -FilePath $py[0] -ArgumentList (@($py[1..($py.Length-1)] + @("-m", "backend.shell.cursor_llm_stub"))) -WindowStyle Hidden
}

Set-Location $ChassisRoot
& bun run --cwd packages/opencode --conditions=browser src/index.ts @args
exit $LASTEXITCODE
