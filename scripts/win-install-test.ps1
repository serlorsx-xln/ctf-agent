# Remote Windows fresh-install smoke (called from scripts/test-install.sh).
$ErrorActionPreference = "Stop"
$src = Join-Path $env:USERPROFILE "Downloads\Artemis-Install-test.ps1"
if (-not (Test-Path $src)) { throw "Missing $src" }
$t = Join-Path $env:TEMP ("artemis-fresh-" + [guid]::NewGuid().ToString("N"))
$env:ARTEMIS_HOME = $t
Write-Host "==> Windows fresh install to $t"
& powershell -ExecutionPolicy Bypass -File $src -SkipDocker -NoPause
if (-not (Test-Path (Join-Path $t ".venv"))) { throw "no .venv after install" }
& powershell -ExecutionPolicy Bypass -File (Join-Path $t "scripts\verify-install.ps1") -SkipDocker
Remove-Item -Recurse -Force $t -ErrorAction SilentlyContinue
Remove-Item -Force $src -ErrorAction SilentlyContinue
Write-Host "WIN_OK"
