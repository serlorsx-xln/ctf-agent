@echo off
setlocal EnableExtensions
REM Artemis TUI launcher for Windows.
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

set "PY="
if exist "%REPO%\.venv\Scripts\python.exe" set "PY=%REPO%\.venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.local\bin\uv.exe" set "PY=UV"
if not defined PY (
  echo Run scripts/install.ps1 first.
  exit /b 1
)

REM Pre-TUI setup gate (terminal prompt + logs; TUI starts only after).
if "%PY%"=="UV" (
  "%USERPROFILE%\.local\bin\uv.exe" run --directory "%REPO%" python "%REPO%\scripts\pre_tui_setup.py"
) else (
  "%PY%" "%REPO%\scripts\pre_tui_setup.py"
)

set "CREDS_FILE=%TEMP%\artemis-creds-%RANDOM%.env"
set "BOOT_ERR=%USERPROFILE%\.cache\artemis\logs\bootstrap.err"
if not exist "%USERPROFILE%\.cache\artemis\logs" mkdir "%USERPROFILE%\.cache\artemis\logs" >nul 2>&1
if "%PY%"=="UV" (
  "%USERPROFILE%\.local\bin\uv.exe" run --directory "%REPO%" python "%REPO%\scripts\tui_bootstrap.py" --emit cmd > "%CREDS_FILE%" 2> "%BOOT_ERR%"
) else (
  "%PY%" "%REPO%\scripts\tui_bootstrap.py" --emit cmd > "%CREDS_FILE%" 2> "%BOOT_ERR%"
)
if exist "%CREDS_FILE%" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%CREDS_FILE%") do set "%%A=%%B"
  del "%CREDS_FILE%" 2>nul
)
for %%A in ("%BOOT_ERR%") do if %%~zA GTR 0 echo artemis: bootstrap warnings (see %BOOT_ERR%)

cd /d "%ROOT%"
REM Prefer prebuilt binary (fast). Set ARTEMIS_TUI_DEV=1 to force bun src.
set "TUI_BIN="
if defined OPENCODE_BIN_PATH if exist "%OPENCODE_BIN_PATH%" set "TUI_BIN=%OPENCODE_BIN_PATH%"
if not defined TUI_BIN if not "%ARTEMIS_TUI_DEV%"=="1" (
  if /I "%PROCESSOR_ARCHITECTURE%"=="ARM64" (
    set "TUI_CAND=%ROOT%\packages\opencode\dist\opencode-windows-arm64\bin\opencode.exe"
  ) else (
    set "TUI_CAND=%ROOT%\packages\opencode\dist\opencode-windows-x64\bin\opencode.exe"
  )
)
if not defined TUI_BIN if defined TUI_CAND if exist "%TUI_CAND%" set "TUI_BIN=%TUI_CAND%"
if defined TUI_BIN (
  "%TUI_BIN%" %*
) else (
  echo artemis: no prebuilt TUI — using bun src (slow). Run: bash scripts/build-tui.sh 1>&2
  "%BUN%" --cwd packages/opencode --conditions=browser src/index.ts %*
)
set "EC=%ERRORLEVEL%"
exit /b %EC%
