$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    py -3 -m venv .venv
}
& '.venv\Scripts\python.exe' -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
    Write-Host 'Created .env. Enter the bot token and your Telegram user ID.'
} else {
    Write-Host 'Existing .env was kept.'
}

Write-Host 'Done. After editing .env, run .venv\Scripts\python.exe bot.py'
Write-Host 'For autostart: .\install_autostart.ps1'
