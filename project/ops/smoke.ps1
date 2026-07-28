param(
    [string]$BaseUrl = $env:PUBLIC_BASE_URL,
    [string]$WebhookSecret = $env:TELEGRAM_WEBHOOK_SECRET,
    [string]$AdminUser = $env:ADMIN_USERNAME,
    [string]$AdminPassword = $env:ADMIN_PASSWORD
)

if (-not $BaseUrl) { throw "PUBLIC_BASE_URL is required" }
if (-not $WebhookSecret) { throw "TELEGRAM_WEBHOOK_SECRET is required" }

function Assert-Ok($Name, $Response) {
    if ($Response.StatusCode -lt 200 -or $Response.StatusCode -ge 400) {
        throw "$Name failed with HTTP $($Response.StatusCode)"
    }
}

$webhookProbe = Invoke-WebRequest -Method Post -Uri "$BaseUrl/telegram/webhook" -Headers @{"X-Telegram-Bot-Api-Secret-Token"=$WebhookSecret} -Body '{"update_id":1}' -ContentType "application/json" -SkipHttpErrorCheck
if ($webhookProbe.StatusCode -notin @(200, 202)) {
    throw "webhook health probe returned HTTP $($webhookProbe.StatusCode)"
}

$privacy = Invoke-WebRequest -Method Get -Uri "$BaseUrl/admin/login" -SkipHttpErrorCheck
Assert-Ok "privacy/admin login page" $privacy

$faq = Invoke-WebRequest -Method Get -Uri "$BaseUrl/admin/login?smoke=faq" -SkipHttpErrorCheck
Assert-Ok "faq smoke" $faq

$booking = Invoke-WebRequest -Method Get -Uri "$BaseUrl/admin/login?smoke=booking" -SkipHttpErrorCheck
Assert-Ok "booking smoke" $booking

if ($AdminUser -and $AdminPassword) {
    $login = Invoke-WebRequest -Method Post -Uri "$BaseUrl/admin/login" -Body @{username=$AdminUser; password=$AdminPassword} -SkipHttpErrorCheck
    if ($login.StatusCode -notin @(200, 302, 303)) {
        throw "admin login smoke failed with HTTP $($login.StatusCode)"
    }
}

Write-Output "smoke passed"
