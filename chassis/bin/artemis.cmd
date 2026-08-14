@echo off
setlocal EnableExtensions
REM Artemis TUI launcher for Windows — no blocking waits before UI.
if defined SystemRoot set "PATH=%SystemRoot%\System32;%SystemRoot%\System32\WindowsPowerShell\v1.0;%PATH%"
set "PATH=%USERPROFILE%\.bun\bin;%USERPROFILE%\.local\bin;%PATH%"
set "ROOT=%~dp0.."
set "REPO=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
for %%I in ("%REPO%") do set "REPO=%%~fI"
set "ARTEMIS=1"
set "ARTEMIS_CHASSIS_ROOT=%ROOT%"
set "ARTEMIS_REPO_ROOT=%REPO%"
if not defined OPENCODE_CONFIG set "OPENCODE_CONFIG=%ROOT%\opencode.json"
set "PYTHONIOENCODING=utf-8"

set "BUN="
if exist "%USERPROFILE%\.bun\bin\bun.exe" set "BUN=%USERPROFILE%\.bun\bin\bun.exe"
if not defined BUN if defined BUN_INSTALL if exist "%BUN_INSTALL%\bin\bun.exe" set "BUN=%BUN_INSTALL%\bin\bun.exe"
if not defined BUN (
  where bun >nul 2>&1 && for /f "delims=" %%B in ('where bun') do if not defined BUN set "BUN=%%B"
)
if not defined BUN (
  echo Artemis TUI requires Bun. Install: https://bun.sh
  exit /b 1
)

set "CREDS_FILE=%TEMP%\artemis-creds-%RANDOM%.env"
set "BOOT_ERR=%USERPROFILE%\.cache\artemis\logs\bootstrap.err"
if not exist "%USERPROFILE%\.cache\artemis\logs" mkdir "%USERPROFILE%\.cache\artemis\logs" >nul 2>&1
if exist "%REPO%\.venv\Scripts\python.exe" (
  "%REPO%\.venv\Scripts\python.exe" "%REPO%\scripts\tui_bootstrap.py" --emit cmd > "%CREDS_FILE%" 2> "%BOOT_ERR%"
) else if exist "%USERPROFILE%\.local\bin\uv.exe" (
  "%USERPROFILE%\.local\bin\uv.exe" run --directory "%REPO%" python "%REPO%\scripts\tui_bootstrap.py" --emit cmd > "%CREDS_FILE%" 2> "%BOOT_ERR%"
) else (
  echo Run scripts/install.ps1 first.
  exit /b 1
)
if exist "%CREDS_FILE%" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%CREDS_FILE%") do set "%%A=%%B"
  del "%CREDS_FILE%" 2>nul
)
for %%A in ("%BOOT_ERR%") do if %%~zA GTR 0 echo artemis: bootstrap warnings (see %BOOT_ERR%)

cd /d "%ROOT%"
"%BUN%" --cwd packages/opencode --conditions=browser src/index.ts %*
set "EC=%ERRORLEVEL%"
exit /b %EC%
