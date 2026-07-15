$ErrorActionPreference = "Stop"

Set-Location -LiteralPath (Resolve-Path (Join-Path $PSScriptRoot "..\.."))

Remove-Item Env:POSTGRES_USER, Env:POSTGRES_PASSWORD, Env:POSTGRES_DB, Env:REDIS_PASSWORD, Env:REDIS_URL -ErrorAction SilentlyContinue
$env:DATABASE_URL = ""
$suffix = [guid]::NewGuid().ToString("N")
$env:RABBITMQ_USER = "task5_$suffix"
$env:RABBITMQ_PASSWORD = [guid]::NewGuid().ToString("N")
$env:RABBITMQ_URL = "amqp://$($env:RABBITMQ_USER):$($env:RABBITMQ_PASSWORD)@rabbitmq:5672/"

docker compose --env-file ../.env config --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Canonical Compose config rejected PostgreSQL-parts fallback"
}
