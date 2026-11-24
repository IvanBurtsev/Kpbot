# Запуск прототипа kpbot
param(
    [int]$Port = 8000,
    [switch]$Demo
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

Set-Location -Path $PSScriptRoot

if (Test-Path (Join-Path $PSScriptRoot '.env')) {
    Write-Host 'Конфигурация: .env' -ForegroundColor DarkGray
} else {
    Write-Host 'Конфигурация: значения по умолчанию (см. .env.example)' -ForegroundColor Yellow
}

if ($Demo) {
    python -m kpbot.cli seed
    python -m kpbot.cli demo --fresh
    exit $LASTEXITCODE
}

if (-not (Test-Path (Join-Path $PSScriptRoot 'data\price_list.xlsx'))) {
    Write-Host 'Прайс не найден — создаю демо-прайс и письма' -ForegroundColor Yellow
    python -m kpbot.cli seed
}

Write-Host ''
Write-Host "Интерфейс: http://127.0.0.1:$Port/ui" -ForegroundColor Green
Write-Host "API:       http://127.0.0.1:$Port/api/docs" -ForegroundColor Green
Write-Host ''

python -m kpbot.cli serve --port $Port