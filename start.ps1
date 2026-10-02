param(
    [ValidateSet('Setup','Check','Run','Test')][string]$Action = 'Run',
    [string]$PythonCommand = 'python'
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [Console]::OutputEncoding
Set-Location -LiteralPath $PSScriptRoot
$TaskBotPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if ($Action -eq 'Setup') {
    & $PythonCommand -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python.' }
    & $TaskBotPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости.' }
    if (-not (Test-Path -LiteralPath '.env')) {
        Copy-Item -LiteralPath '.env.example' -Destination '.env'
    }
    New-Item -ItemType Directory -Force -Path secrets, data | Out-Null
    Write-Output 'Готово. Заполните .env и сохраните ключ в secrets/google-service-account.json.'
    exit 0
}
if (-not (Test-Path -LiteralPath $TaskBotPython)) { throw 'Сначала выполните ./start.ps1 Setup' }
switch ($Action) {
    'Check' { & $TaskBotPython bot.py check }
    'Test'  { & $TaskBotPython -m unittest -v test_bot }
    'Run'   { & $TaskBotPython bot.py run }
}
exit $LASTEXITCODE
