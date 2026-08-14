# User PATH + global `artemis` command on Windows.
# Dot-source: . (Join-Path $PSScriptRoot 'lib\windows-path.ps1')

function Set-ArtemisInstallPath {
  param([Parameter(Mandatory)][string]$RepoRoot)
  $f = Join-Path $env:USERPROFILE ".local\share\artemis\install-path.txt"
  $dir = Split-Path $f -Parent
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $path = $RepoRoot.TrimEnd('\')
  Set-Content -Path $f -Value $path -Encoding ASCII
}

function Ensure-UserPathContains {
  param([Parameter(Mandatory)][string]$Dir)
  if (-not (Test-Path $Dir)) {
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  }
  $normalized = [IO.Path]::GetFullPath($Dir)
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if (-not $userPath) { $userPath = "" }
  $parts = $userPath -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
  if ($parts -contains $normalized) { return $false }
  $newPath = if ($userPath.Trim()) { "$userPath;$normalized" } else { $normalized }
  [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
  if ($env:Path -notlike "*$normalized*") {
    $env:Path = "$env:Path;$normalized"
  }
  return $true
}

function Install-ArtemisCli {
  param([Parameter(Mandatory)][string]$RepoRoot)

  Set-ArtemisInstallPath $RepoRoot
  $localBin = Join-Path $env:USERPROFILE ".local\bin"
  New-Item -ItemType Directory -Force -Path $localBin | Out-Null
  $added = Ensure-UserPathContains $localBin
  if ($added) {
    Write-Host "==> Added $localBin to user PATH (open a new terminal for artemis)" -ForegroundColor Cyan
  }

  $artemisCmd = Join-Path $localBin "artemis.cmd"
  @"
@echo off
setlocal EnableExtensions
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.bun\bin;%PATH%"
set "PATHFILE=%USERPROFILE%\.local\share\artemis\install-path.txt"
if defined ARTEMIS_REPO_ROOT if exist "%ARTEMIS_REPO_ROOT%\chassis\bin\artemis.cmd" (
  set "REPO=%ARTEMIS_REPO_ROOT%"
  goto run
)
if exist "%PATHFILE%" (
  set /p REPO=<"%PATHFILE%"
) else (
  set "REPO=%USERPROFILE%\artemis"
)
:run
if not exist "%REPO%\chassis\bin\artemis.cmd" (
  echo Artemis repo not found. Run scripts\install.ps1 from the checkout.
  exit /b 1
)
if "%~1"=="" goto tui
if /i "%~1"=="swarm" goto py
if /i "%~1"=="setup" goto py
if /i "%~1"=="chassis" goto py
if /i "%~1"=="--help" goto py
if /i "%~1"=="-h" goto py
goto tui
:tui
call "%REPO%\chassis\bin\artemis.cmd" %*
exit /b %ERRORLEVEL%
:py
cd /d "%REPO%"
if exist "%REPO%\.venv\Scripts\python.exe" (
  "%REPO%\.venv\Scripts\python.exe" -m backend.cli %*
) else (
  "%USERPROFILE%\.local\bin\uv.exe" run --directory "%REPO%" artemis %*
)
exit /b %ERRORLEVEL%
"@ | Set-Content -Path $artemisCmd -Encoding ASCII

  Write-Host "==> Global command installed: artemis" -ForegroundColor Cyan
  Write-Host "    (open a NEW terminal/cmd, then type: artemis)" -ForegroundColor DarkGray
}
