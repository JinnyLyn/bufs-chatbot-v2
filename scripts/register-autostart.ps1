# register-autostart.ps1 — run the whole stack automatically at logon via Task Scheduler.
# Run once (normal user is fine). Unregister with the command printed at the end.

$start = Join-Path $PSScriptRoot "start-all.ps1"
$taskName = "AgenticRAG-Stack"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$start`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Hours 0)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Start agentic-RAG stack: local Ollama (:11435) + backend (:8000) + frontend (:3000)" -Force | Out-Null

Write-Host "Registered scheduled task '$taskName' — runs scripts\start-all.ps1 at logon."
Write-Host "Run now:    Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Remove:     Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false"
