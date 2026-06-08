# stop-all.ps1 — stop frontend (:3000), backend (:8000), local Ollama (:11435).
# Leaves the SSH tunnel on :11434 (remote Ollama) untouched.

foreach ($port in 3000, 8000, 11435) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        $conns | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
            try { Stop-Process -Id $_ -Force -ErrorAction Stop; Write-Host "stopped :$port (PID $_)" } catch {}
        }
    }
    else { Write-Host ":$port not running" }
}
Write-Host "Note: SSH tunnel :11434 (remote Ollama) left untouched."
