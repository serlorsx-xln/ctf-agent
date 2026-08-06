@echo off
setlocal
REM Artemis TUI launcher for Windows (Docker Desktop + Bun + uv).
set "ROOT=%~dp0.."
set "REPO=%~dp0..\.."
set "ARTEMIS=1"
set "ARTEMIS_CHASSIS_ROOT=%ROOT%"
set "ARTEMIS_REPO_ROOT=%REPO%"
if not defined OPENCODE_CONFIG set "OPENCODE_CONFIG=%ROOT%\opencode.json"
if not defined DOCKER_HOST set "DOCKER_HOST=npipe:////./pipe/docker_engine"

where bun >nul 2>&1 || (
  echo Artemis TUI requires Bun. Install: https://bun.sh
  exit /b 1
)
where uv >nul 2>&1 || (
  echo Artemis requires uv. Install: https://docs.astral.sh/uv/
  exit /b 1
)

if exist "%REPO%\.venv\Scripts\python.exe" (
  set "PY=%REPO%\.venv\Scripts\python.exe"
) else (
  set "PY=uv run --directory %REPO% python"
)

for /f "delims=" %%K in ('%PY% -c "from backend.shell.credentials import read_tui_api_keys; [print('%s=%%s' %% (k,v)) for k,v in read_tui_api_keys().items()]" 2^>nul') do set "%%K"

%PY% -c "from backend.daemon.transport import daemon_alive; import sys; sys.exit(0 if daemon_alive() else 1)" >nul 2>&1
if errorlevel 1 (
  start /b "" %PY% -m backend.daemon.server > "%TEMP%\artemis-daemon.log" 2>&1
)

curl -sf --max-time 1 http://127.0.0.1:18765/v1/models >nul 2>&1
if errorlevel 1 (
  start /b "" %PY% -m backend.shell.cursor_llm_stub > "%TEMP%\artemis-cursor-llm-stub.log" 2>&1
)

cd /d "%ROOT%"
bun run --cwd packages/opencode --conditions=browser src/index.ts %*
set "EC=%ERRORLEVEL%"
exit /b %EC%
