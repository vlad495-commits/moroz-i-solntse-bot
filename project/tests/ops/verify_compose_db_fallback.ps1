$ErrorActionPreference = "Stop"

Set-Location -LiteralPath (Resolve-Path (Join-Path $PSScriptRoot "..\.."))

Remove-Item Env:POSTGRES_USER, Env:POSTGRES_PASSWORD, Env:POSTGRES_DB, Env:REDIS_PASSWORD, Env:REDIS_URL -ErrorAction SilentlyContinue
$env:DATABASE_URL = ""
$suffix = [guid]::NewGuid().ToString("N")
$env:RABBITMQ_USER = "task5_$suffix"
$env:RABBITMQ_PASSWORD = [guid]::NewGuid().ToString("N")
$env:RABBITMQ_URL = "amqp://$($env:RABBITMQ_USER):$($env:RABBITMQ_PASSWORD)@rabbitmq:5672/"

docker compose --env-file ../.env --profile yclients-smoke config --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Canonical Compose config rejected PostgreSQL-parts fallback"
}

$renderedConfig = docker compose --env-file ../.env --profile yclients-smoke config --format json
if ($LASTEXITCODE -ne 0) {
    throw "Canonical Compose config could not be rendered as JSON"
}
$services = ($renderedConfig -join "`n" | ConvertFrom-Json).services
$expectedEnvironment = @{
    worker = @(
        "BUSINESS_ALERT_CHAT_ID",
        "COMPACT_KEEP_RECENT",
        "COMPACT_MAX_TOKENS",
        "COMPACT_THRESHOLD",
        "CONTEXT_MESSAGES_LIMIT",
        "DATA_RETENTION_DAYS",
        "DATABASE_URL",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MAX_TOKENS",
        "LLM_MODEL",
        "LLM_REQUEST_TIMEOUT_SEC",
        "LLM_TEMPERATURE",
        "OPENAI_API_KEY",
        "OUTPUT_VALIDATOR_ENABLED",
        "POSTGRES_DB",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "RABBITMQ_URL",
        "REDIS_URL",
        "RESERVE_API_KEY",
        "RESERVE_BASE_URL",
        "RESERVE_MODEL",
        "ROUTER_API_KEY",
        "ROUTER_BASE_URL",
        "ROUTER_MAX_TOKENS",
        "ROUTER_MODEL",
        "SECURITY_API_KEY",
        "SECURITY_BASE_URL",
        "SECURITY_MAX_TOKENS",
        "SECURITY_MODEL",
        "STAFF_TELEGRAM_CHAT_ID",
        "TELEGRAM_BOT_TOKEN",
        "TECHNICAL_ALERT_CHAT_ID",
        "YCLIENTS_BASE_URL",
        "YCLIENTS_CATALOG_GROUNDING_ENABLED",
        "YCLIENTS_COMPANY_ID",
        "YCLIENTS_PARTNER_TOKEN",
        "YCLIENTS_TIMEOUT_SECONDS",
        "YCLIENTS_TIMEZONE",
        "YCLIENTS_USER_TOKEN"
    )
    "yclients-smoke" = @(
        "YCLIENTS_BASE_URL",
        "YCLIENTS_COMPANY_ID",
        "YCLIENTS_PARTNER_TOKEN",
        "YCLIENTS_SANDBOX_CONSENT",
        "YCLIENTS_TEST_NAME",
        "YCLIENTS_TEST_PHONE",
        "YCLIENTS_TEST_SERVICE_ID",
        "YCLIENTS_TIMEOUT_SECONDS",
        "YCLIENTS_TIMEZONE",
        "YCLIENTS_USER_TOKEN"
    )
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

$yclientsKeys = @(
    "YCLIENTS_BASE_URL",
    "YCLIENTS_COMPANY_ID",
    "YCLIENTS_PARTNER_TOKEN",
    "YCLIENTS_SANDBOX_CONSENT",
    "YCLIENTS_TEST_NAME",
    "YCLIENTS_TEST_PHONE",
    "YCLIENTS_TEST_SERVICE_ID",
    "YCLIENTS_TIMEOUT_SECONDS",
    "YCLIENTS_TIMEZONE",
    "YCLIENTS_USER_TOKEN"
)
foreach ($serviceProperty in $services.PSObject.Properties) {
    $serviceName = $serviceProperty.Name
    $service = $serviceProperty.Value
    if ($service.PSObject.Properties.Name -contains "env_file") {
        throw "Rendered Compose service still contains env_file: $serviceName"
    }
    if ($serviceName -notin @("worker", "yclients-smoke")) {
        $leakedKeys = @($service.environment.PSObject.Properties.Name | Where-Object {
            $_ -in $yclientsKeys
        })
        if ($leakedKeys.Count -ne 0) {
            throw "Rendered Compose leaked YCLIENTS environment: $serviceName"
        }
    }
}

$requiredRuntimeKeys = @{
    bot = @("DATABASE_URL", "POSTGRES_DB", "POSTGRES_PASSWORD", "POSTGRES_USER")
    admin = @("DATABASE_URL", "POSTGRES_DB", "POSTGRES_PASSWORD", "POSTGRES_USER")
}
foreach ($serviceName in $requiredRuntimeKeys.Keys) {
    $actualKeys = @($services.$serviceName.environment.PSObject.Properties.Name)
    $missingKeys = @($requiredRuntimeKeys[$serviceName] | Where-Object { $_ -notin $actualKeys })
    if ($missingKeys.Count -ne 0) {
        throw "Rendered Compose runtime fallback mismatch: $serviceName"
    }
}
