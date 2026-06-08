# rollback-tuning.ps1 — instantly revert the 2026-06-05 speed tuning.
# Restores the pre-tuning .env (caps 8/10, MAX_PARENT_SIZE 6000) AND the pre-tuning KB
# (qdrant_db + parent_store) from backups\pretune — so NO re-embedding is needed.
# Then relaunches the backend.

$ErrorActionPreference = "Stop"
$Repo = Split-Path $PSScriptRoot -Parent
$bak = Join-Path $Repo "backups\pretune"
if (-not (Test-Path $bak)) { Write-Error "No backup found at $bak — cannot roll back."; exit 1 }

# 1) stop backend (free the Qdrant lock)
$c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($c) { $c | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { try { Stop-Process -Id $_ -Force } catch {} } }
Start-Sleep -Seconds 2

# 2) restore .env + KB from the backup
Copy-Item (Join-Path $bak ".env") (Join-Path $Repo "project\.env") -Force
foreach ($d in @("qdrant_db", "parent_store")) {
    $dst = Join-Path $Repo $d
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
    Copy-Item (Join-Path $bak $d) $dst -Recurse -Force
}
Write-Host "Restored pre-tuning .env + KB (qdrant_db + parent_store) from backups\pretune."

# 3) relaunch backend
$env:OLLAMA_HOST = "127.0.0.1:11435"
New-Item -ItemType Directory -Force -Path (Join-Path $Repo "logs\backend") | Out-Null
Start-Process -FilePath "python" -ArgumentList "project\server.py" -WorkingDirectory $Repo `
    -RedirectStandardOutput (Join-Path $Repo "logs\backend\server.out") `
    -RedirectStandardError  (Join-Path $Repo "logs\backend\server.err") -WindowStyle Hidden
Write-Host "Backend relaunching. Check: curl http://localhost:8000/health  (model load ~30-50s)"
