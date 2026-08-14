# Shared Docker Desktop helpers for Windows (install / QA / TUI / swarm).
# Dot-source: . (Join-Path $PSScriptRoot 'lib\windows-docker.ps1')

function Add-ArtemisDockerToPath {
    $stubDir = Join-Path $env:USERPROFILE "bin"
    $dockerBin = "C:\Program Files\Docker\Docker\resources\bin"
    $prefix = @()
    if (Test-Path $stubDir) { $prefix += $stubDir }
    if (Test-Path $dockerBin) { $prefix += $dockerBin }
    if ($prefix.Count -gt 0) {
        $joined = ($prefix -join ";")
        if ($env:Path -notlike "*$joined*") {
            $env:Path = "$joined;$env:Path"
        }
    }
}

function Ensure-ArtemisDockerBuildConfig {
    $stubDir = Join-Path $env:USERPROFILE "bin"
    New-Item -ItemType Directory -Force -Path $stubDir | Out-Null
    $stubPs1 = Join-Path $stubDir "docker-credential-stub.ps1"
    if (-not (Test-Path $stubPs1)) {
        @'
param([string]$Action)
switch ($Action) {
  'get' { [Console]::Out.Write('{"Username":"","Secret":""}'); exit 0 }
  'erase' { exit 0 }
  'list' { [Console]::Out.Write('{}'); exit 0 }
  'store' { exit 0 }
  default { exit 1 }
}
'@ | Set-Content $stubPs1 -Encoding UTF8
        Set-Content (Join-Path $stubDir "docker-credential-stub.cmd") '@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\bin\docker-credential-stub.ps1" %*' -Encoding ASCII
    }
    $cfg = Join-Path $env:USERPROFILE ".docker-build"
    New-Item -ItemType Directory -Force -Path $cfg | Out-Null
    @'
{
  "auths": {},
  "credHelpers": {
    "docker.io": "stub",
    "index.docker.io": "stub",
    "https://index.docker.io/v1/": "stub"
  },
  "currentContext": "desktop-linux"
}
'@ | Set-Content (Join-Path $cfg "config.json") -Encoding ASCII
    return $cfg
}

function Test-ArtemisDockerDaemon {
    Add-ArtemisDockerToPath
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }

    $prevCfg = $env:DOCKER_CONFIG
    $prevHost = $env:DOCKER_HOST
    try {
        Remove-Item Env:DOCKER_CONFIG -ErrorAction SilentlyContinue
        Remove-Item Env:DOCKER_HOST -ErrorAction SilentlyContinue
        cmd /c "docker info >nul 2>&1"
        return ($LASTEXITCODE -eq 0)
    } finally {
        if ($null -eq $prevCfg) {
            Remove-Item Env:DOCKER_CONFIG -ErrorAction SilentlyContinue
        } else {
            $env:DOCKER_CONFIG = $prevCfg
        }
        if ($null -eq $prevHost) {
            Remove-Item Env:DOCKER_HOST -ErrorAction SilentlyContinue
        } else {
            $env:DOCKER_HOST = $prevHost
        }
    }
}

function Invoke-ArtemisDocker {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$DockerArgs
    )
    Add-ArtemisDockerToPath
    Ensure-ArtemisDockerBuildConfig | Out-Null
    & docker @DockerArgs
    return $LASTEXITCODE
}

function Initialize-ArtemisDockerCli {
    # Path + build stub files only. Do not override DOCKER_CONFIG globally —
    # that breaks `docker info` while Docker Desktop is already running.
    Add-ArtemisDockerToPath
    Ensure-ArtemisDockerBuildConfig | Out-Null
}

function Wait-ArtemisDockerReady {
    param([int]$MaxWaitSeconds = 120)
    Add-ArtemisDockerToPath

    if (Test-ArtemisDockerDaemon) { return $true }

    Start-Service com.docker.service -ErrorAction SilentlyContinue
    $dd = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if ((Test-Path $dd) -and -not (Get-Process "Docker Desktop" -ErrorAction SilentlyContinue)) {
        Start-Process $dd -ErrorAction SilentlyContinue
    }

    $deadline = [DateTime]::UtcNow.AddSeconds($MaxWaitSeconds)
    $i = 0
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-ArtemisDockerDaemon) { return $true }
        $sleep = if ($i -lt 6) { 1 } else { 3 }
        Start-Sleep -Seconds $sleep
        $i++
    }
    return $false
}

function Install-ArtemisDockerDesktop {
    if (Get-Command docker -ErrorAction SilentlyContinue) { return $true }
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install Docker.DockerDesktop `
            --accept-package-agreements --accept-source-agreements --silent 2>$null | Out-Null
        return (Get-Command docker -ErrorAction SilentlyContinue)
    }
    Write-Host "  Download Docker Desktop: https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe"
    Write-Host "  Or install winget, then re-run install."
    return $false
}
