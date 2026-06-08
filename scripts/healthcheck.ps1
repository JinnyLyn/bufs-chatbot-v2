# healthcheck.ps1 — probe backend /health + /health/llm + frontend. Exit 1 if anything is down.
# Usable from Task Scheduler / external monitoring.

$ok = $true

try {
    $h = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 5
    Write-Host ("[backend ] ok   model={0}  ollama={1}  kb_docs={2}  langfuse={3}  uptime={4}s" -f `
        $h.model, $h.ollama_base_url, $h.kb_docs, $h.langfuse_enabled, $h.uptime_s)
}
catch { Write-Host "[backend ] DOWN"; $ok = $false }

try {
    $l = Invoke-RestMethod -Uri "http://localhost:8000/health/llm" -TimeoutSec 8
    if ($l.status -eq "ok") {
        if ($l.loaded_models.Count -eq 0) { Write-Host "[llm/gpu ] no model loaded (loads on first query)" }
        foreach ($m in $l.loaded_models) {
            Write-Host ("[llm/gpu ] {0}  gpu={1}%  vram={2}MB" -f $m.name, $m.gpu_offload_pct, $m.vram_mb)
        }
    }
    else { Write-Host "[llm/gpu ] ollama unreachable at $($l.ollama_base_url)" }
}
catch { Write-Host "[llm/gpu ] n/a" }

if (Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "[frontend] ok   :3000"
}
else { Write-Host "[frontend] DOWN"; $ok = $false }

if (-not $ok) { exit 1 }
Write-Host "ALL OK"
