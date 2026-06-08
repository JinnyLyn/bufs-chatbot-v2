# start-all.ps1 — start the full stack: local Ollama (:11435), backend (:8000), frontend (:3000).
# Idempotent: skips anything already listening. Logs go under <repo>\logs.
# Leaves the user's SSH tunnel on :11434 (remote Ollama) untouched.

$ErrorActionPreference = "Stop"
$Repo = Split-Path $PSScriptRoot -Parent
$LogDir = Join-Path $Repo "logs"
foreach ($d in @("ollama", "backend", "frontend")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $LogDir $d) | Out-Null
}

# The local Ollama server must bind a real address on a dedicated port (NOT the tunnel).
$env:OLLAMA_HOST = "127.0.0.1:11435"

function Test-Port($port) {
    $null -ne (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}
function Wait-Port($port, $timeoutSec = 60) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $timeoutSec) {
        if (Test-Port $port) { return $true }; Start-Sleep -Seconds 2
    }
    return $false
}
function Wait-Http($url, $timeoutSec = 120) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $timeoutSec) {
        try { if ((Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 5).StatusCode -eq 200) { return $true } } catch {}
        Start-Sleep -Seconds 3
    }
    return $false
}

# 1) Local Ollama (RTX 4070)
if (Test-Port 11435) { Write-Host "[ok]    Ollama already on :11435" }
else {
    Write-Host "[start] local Ollama :11435"
    Start-Process -FilePath "ollama" -ArgumentList "serve" `
        -RedirectStandardOutput (Join-Path $LogDir "ollama\ollama.out") `
        -RedirectStandardError  (Join-Path $LogDir "ollama\ollama.err") -WindowStyle Hidden
    Wait-Port 11435 30 | Out-Null
}

# 2) Backend (FastAPI) — wait on /health (only 200 after model load)
if (Test-Port 8000) { Write-Host "[ok]    backend already on :8000" }
else {
    Write-Host "[start] backend :8000"
    Start-Process -FilePath "python" -ArgumentList "project\server.py" -WorkingDirectory $Repo `
        -RedirectStandardOutput (Join-Path $LogDir "backend\server.out") `
        -RedirectStandardError  (Join-Path $LogDir "backend\server.err") -WindowStyle Hidden
}

# 3) Frontend (Next.js dev)
if (Test-Port 3000) { Write-Host "[ok]    frontend already on :3000" }
else {
    Write-Host "[start] frontend :3000 (npm run dev)"
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "npm run dev" `
        -WorkingDirectory (Join-Path $Repo "frontend") `
        -RedirectStandardOutput (Join-Path $LogDir "frontend\frontend.out") `
        -RedirectStandardError  (Join-Path $LogDir "frontend\frontend.err") -WindowStyle Hidden
}

$backendUp = Wait-Http "http://localhost:8000/health" 120
$frontendUp = Wait-Port 3000 60

Write-Host ""
Write-Host "Ollama   :11435 -> $(Test-Port 11435)"
Write-Host "Backend  :8000  -> $backendUp    (health: http://localhost:8000/health)"
Write-Host "Frontend :3000  -> $frontendUp    (open:   http://localhost:3000)"
if (-not ($backendUp -and $frontendUp)) { Write-Warning "Some services not ready — check logs\ for details."; exit 1 }
