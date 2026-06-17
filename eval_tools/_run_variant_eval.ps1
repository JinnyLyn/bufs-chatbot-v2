# _run_variant_eval.ps1 — run combined88 e2e for ONE sparse-tokenizer variant.
# Starts the worktree backend (:8000) with the variant's sparse env, runs the eval,
# snapshots the result, re-scores it (offline scorer-fix), then stops the backend.
# Session tool for the BM25-tokenizer A/B — not meant to be committed.
#
#   powershell -ExecutionPolicy Bypass -File eval_tools\_run_variant_eval.ps1 `
#       -Model kiwi -Idf true -Collection document_child_chunks__kiwi_idf -Label V2_kiwi_idf
param(
    [Parameter(Mandatory = $true)][string]$Model,
    [Parameter(Mandatory = $true)][string]$Idf,
    [Parameter(Mandatory = $true)][string]$Collection,
    [Parameter(Mandatory = $true)][string]$Label
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path $PSScriptRoot -Parent
$LogDir = Join-Path $Repo "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Free :8000 if something is already listening (stale backend).
$busy = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($busy) { $busy.OwningProcess | Sort-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } ; Start-Sleep 2 }

# Variant env (explicit env beats .env: python-dotenv load_dotenv override=False).
$env:SPARSE_MODEL = $Model
$env:SPARSE_IDF = $Idf
$env:CHILD_COLLECTION = $Collection
if ($Model -eq "okt") { $env:JAVA_HOME = "C:\Program Files\Eclipse Adoptium\jdk-17.0.19.10-hotspot" }

Write-Host "[$Label] starting backend  (SPARSE_MODEL=$Model SPARSE_IDF=$Idf COLLECTION=$Collection)"
$out = Join-Path $LogDir "backend_$Label.out"
$err = Join-Path $LogDir "backend_$Label.err"
$proc = Start-Process -FilePath "python" -ArgumentList "project\server.py" -WorkingDirectory $Repo `
    -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru

try {
    # Wait for /health (200 only after embeddings + Qdrant + LLM warmup).
    $up = $false
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt 240) {
        try { if ((Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:8000/health" -TimeoutSec 5).StatusCode -eq 200) { $up = $true; break } } catch {}
        if ($proc.HasExited) { throw "backend exited early (code $($proc.ExitCode)) — see $err" }
        Start-Sleep -Seconds 3
    }
    if (-not $up) { throw "backend /health not ready within 240s — see $err" }
    Write-Host "[$Label] backend healthy after $([int]$sw.Elapsed.TotalSeconds)s — running combined88..."

    & python (Join-Path $Repo "eval_tools\_eval_combined88.py")

    # Snapshot the raw result so the next variant doesn't overwrite it.
    Copy-Item (Join-Path $LogDir "combined88_new_result.json") (Join-Path $LogDir "combined88_$Label.json") -Force
    Copy-Item (Join-Path $LogDir "combined88_new.jsonl") (Join-Path $LogDir "combined88_$Label.jsonl") -Force -ErrorAction SilentlyContinue

    # Re-score with the scorer-fix (reads logs/combined88_new_result.json from repo root).
    Write-Host "[$Label] === rescored ==="
    Push-Location $Repo
    & python (Join-Path $Repo "eval_tools\_rescore88.py") | Tee-Object -FilePath (Join-Path $LogDir "rescore_$Label.txt")
    Pop-Location
}
finally {
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    $busy2 = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    if ($busy2) { $busy2.OwningProcess | Sort-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } }
    Write-Host "[$Label] backend stopped."
}
