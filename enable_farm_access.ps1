#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'
$taskName = 'Remote PC Control Bot'
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
$userId = $task.Principal.UserId
if (-not $userId) { throw 'Could not determine the task user.' }

# The farm batch requires administrator rights and must run in the same
# interactive session as the desktop bot. An elevated logon task avoids UAC
# prompts during a remote Telegram request.
try {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Highest
    Set-ScheduledTask -TaskName $taskName -Principal $principal -ErrorAction Stop | Out-Null
    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop

    $updated = Get-ScheduledTask -TaskName $taskName
    if ($updated.Principal.RunLevel -ne 'Highest') { throw 'The task did not receive highest privileges.' }
    Write-Host "Task '$taskName' now runs with highest privileges for $userId."
} catch {
    $logPath = Join-Path $PSScriptRoot 'logs\admin-setup-error.txt'
    $_ | Out-String | Set-Content -LiteralPath $logPath -Encoding UTF8
    Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    throw
}
