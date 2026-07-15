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

$renderedConfig = docker compose --env-file ../.env config --format json
if ($LASTEXITCODE -ne 0) {
    throw "Canonical Compose config could not be rendered as JSON"
}
$services = ($renderedConfig -join "`n" | ConvertFrom-Json).services
$expectedEnvironment = @{
    worker = @("RABBITMQ_URL")
    redis = @("REDIS_PASSWORD")
    postgres = @("POSTGRES_DB", "POSTGRES_PASSWORD", "POSTGRES_USER")
}

foreach ($serviceName in $expectedEnvironment.Keys) {
    $service = $services.$serviceName
    if ($null -eq $service) {
        throw "Rendered Compose config is missing service: $serviceName"
    }
    if ($service.PSObject.Properties.Name -contains "env_file") {
        throw "Rendered Compose service still contains env_file: $serviceName"
    }

    $actualKeys = @($service.environment.PSObject.Properties.Name | Sort-Object)
    $expectedKeys = @($expectedEnvironment[$serviceName] | Sort-Object)
    $difference = @(Compare-Object $expectedKeys $actualKeys)
    if ($difference.Count -ne 0) {
        throw "Rendered Compose environment allowlist mismatch: $serviceName"
    }
}
