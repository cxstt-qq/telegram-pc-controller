#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$bot = Join-Path $root 'bot.py'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'Run install.ps1 first.' }

& $python -c 'from config import read_config; assert read_config().allowed_user_id > 0'
if ($LASTEXITCODE -ne 0) { throw 'Check .env before installing autostart.' }

$taskName = 'Remote PC Control Bot'
$user = "${env:USERDOMAIN}\${env:USERNAME}"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"' + $bot + '"') -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Task '$taskName' installed and started. Logs: $root\logs"
