# rollback-schedule-fix.ps1 — revert the 2026-06-07 schedule/graduation fixes.
# Restores the pre-fix .env + KB (qdrant_db + parent_store) AND the pre-fix code
# (document_chunker.py month forward-fill, prompts.py credit/date rules) from
# backups\pre-schedule-fix — NO re-embedding needed. Then relaunches the backend.

$ErrorActionPreference = "Stop"
$Repo = Split-Path $PSScriptRoot -Parent
$bak = Join-Path $Repo "backups\pre-schedule-fix"
if (-not (Test-Path $bak)) { Write-Error "No backup found at $bak — cannot roll back."; exit 1 }

# 1) stop backend (free the Qdrant lock)
$c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($c) { $c | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { try { Stop-Process -Id $_ -Force } catch {} } }
Start-Sleep -Seconds 2

# 2) restore .env + KB
Copy-Item (Join-Path $bak ".env.bak") (Join-Path $Repo "project\.env") -Force
foreach ($d in @("qdrant_db", "parent_store")) {
    $dst = Join-Path $Repo $d
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
    Copy-Item (Join-Path $bak $d) $dst -Recurse -Force
}
# 3) restore code (chunker + prompts)
Copy-Item (Join-Path $bak "document_chunker.py.bak") (Join-Path $Repo "project\document_chunker.py") -Force
Copy-Item (Join-Path $bak "prompts.py.bak")          (Join-Path $Repo "project\rag_agent\prompts.py") -Force
Write-Host "Restored pre-schedule-fix .env + KB + code (chunker, prompts) from backups\pre-schedule-fix."

# 4) relaunch backend
$env:OLLAMA_HOST = "127.0.0.1:11435"
New-Item -ItemType Directory -Force -Path (Join-Path $Repo "logs\backend") | Out-Null
Start-Process -FilePath "python" -ArgumentList "project\server.py" -WorkingDirectory $Repo `
    -RedirectStandardOutput (Join-Path $Repo "logs\backend\server.out") `
    -RedirectStandardError  (Join-Path $Repo "logs\backend\server.err") -WindowStyle Hidden
Write-Host "Backend relaunching. Check: curl http://localhost:8000/health  (model load ~30-50s)"
