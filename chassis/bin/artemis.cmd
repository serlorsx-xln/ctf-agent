@echo off
setlocal EnableExtensions
REM Artemis TUI launcher for Windows — no blocking waits before UI.
set "ROOT=%~dp0.."
set "REPO=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
for %%I in ("%REPO%") do set "REPO=%%~fI"
set "ARTEMIS=1"
set "ARTEMIS_CHASSIS_ROOT=%ROOT%"
set "ARTEMIS_REPO_ROOT=%REPO%"
if not defined OPENCODE_CONFIG set "OPENCODE_CONFIG=%ROOT%\opencode.json"
set "PYTHONIOENCODING=utf-8"

where bun >nul 2>&1 || (
  echo Artemis TUI requires Bun. Install: https://bun.sh
  exit /b 1
)

if exist "%REPO%\.venv\Scripts\python.exe" (
  set "PYEXE=%REPO%\.venv\Scripts\python.exe"
) else (
  echo Run scripts/install.ps1 first.
  exit /b 1
)

set "CREDS_FILE=%TEMP%\artemis-creds-%RANDOM%.env"
set "BOOT_ERR=%USERPROFILE%\.cache\artemis\logs\bootstrap.err"
if not exist "%USERPROFILE%\.cache\artemis\logs" mkdir "%USERPROFILE%\.cache\artemis\logs" >nul 2>&1
"%PYEXE%" "%REPO%\scripts\tui_bootstrap.py" --emit cmd > "%CREDS_FILE%" 2> "%BOOT_ERR%"
if exist "%CREDS_FILE%" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%CREDS_FILE%") do set "%%A=%%B"
  del "%CREDS_FILE%" 2>nul
)
for %%A in ("%BOOT_ERR%") do if %%~zA GTR 0 echo artemis: bootstrap warnings (see %BOOT_ERR%)

cd /d "%ROOT%"
bun --cwd packages/opencode --conditions=browser src/index.ts %*
set "EC=%ERRORLEVEL%"
exit /b %EC%
