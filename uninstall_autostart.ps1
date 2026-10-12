$ErrorActionPreference = 'Stop'
$taskName = 'Remote PC Control Bot'
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host 'Autostart removed.'
} else {
    Write-Host 'Autostart task not found.'
}
