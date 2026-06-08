# apply-maruvis-tunnel.ps1
# Point the maruvis.co.kr Cloudflare tunnel (maru-ai) at the NEW agentic-rag chatbot:
#   maruvis.co.kr/api/*  -> FastAPI backend  (localhost:8000)
#   maruvis.co.kr/*      -> Next.js frontend (localhost:3000)
#
# This overwrites the cloudflared SERVICE config (C:\ProgramData\Cloudflared\config.yml)
# with the staged ingress and restarts the service. Requires admin — the script
# self-elevates (one UAC prompt).
#
# ROLLBACK (revert to whatever served maruvis before):
#   copy C:\ProgramData\Cloudflared\config.yml.bak-precamchat over config.yml, then
#   Restart-Service cloudflared

# --- self-elevate ---
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $admin) {
    Write-Host "Elevating (approve the UAC prompt)..."
    Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$PSCommandPath`""
    return
}

$ErrorActionPreference = "Stop"
$staged = "C:\Users\suhwa\Desktop\agentic-rag-for-dummies-main\logs\cf-config-new.yml"
$target = "C:\ProgramData\Cloudflared\config.yml"

if (-not (Test-Path $staged)) { Write-Error "Staged config not found: $staged"; Read-Host "Enter to exit"; exit 1 }
# safety backup (only if one doesn't already exist)
if (-not (Test-Path "$target.bak-precamchat")) { Copy-Item $target "$target.bak-precamchat" -Force }

Copy-Item $staged $target -Force
Write-Host "Applied new ingress -> $target"
Restart-Service cloudflared -Force
Start-Sleep -Seconds 5
Get-Service cloudflared | Select-Object Name, Status | Format-Table -AutoSize
Write-Host "`nDone. Now open: https://maruvis.co.kr   (tunnel may take ~10s to reconnect)"
Read-Host "Press Enter to close"
