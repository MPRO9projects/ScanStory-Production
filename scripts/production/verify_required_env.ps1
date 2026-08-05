param(
    [ValidateSet("staging", "production")]
    [string]$Environment = "staging",
    [switch]$ConfirmProductionReadOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail($Message) {
    Write-Error $Message
    exit 1
}

if ($Environment -eq "production" -and -not $ConfirmProductionReadOnly) {
    Fail "Refusing production environment inspection without -ConfirmProductionReadOnly."
}

$required = @(
    "FLASK_SECRET_KEY",
    "DATABASE_URL",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASS",
    "SCANSTORY_DATA_DIR",
    "SCANSTORY_ADMIN_DATA_DIR",
    "FLASK_ENV"
)

$optional = @(
    "SESSION_COOKIE_SECURE",
    "SECURITY_HSTS_ENABLED",
    "SECURITY_CSP_ENABLED",
    "SECURITY_CSP_ENFORCE",
    "SCANSTORY_STATIC_UPLOADS_DIR",
    "BOOTSTRAP_ADMIN_ENABLED",
    "BOOTSTRAP_ADMIN_EMAIL",
    "BOOTSTRAP_ADMIN_PASSWORD",
    "MAIL_FROM",
    "LOG_LEVEL",
    "REDIS_URL",
    "RQ_QUEUE_NAME",
    "RATE_LIMIT_REDIS_URL"
)

$missing = @()
foreach ($name in $required) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        $missing += $name
        Write-Host "${name}: MISSING"
    } else {
        Write-Host "${name}: SET (masked)"
    }
}

foreach ($name in $optional) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        Write-Host "${name}: optional missing"
    } else {
        Write-Host "${name}: SET (masked)"
    }
}

if ($missing.Count -gt 0) {
    Fail "Missing required environment variables: $($missing -join ', ')"
}

if ($Environment -eq "production") {
    if ($env:FLASK_DEBUG -eq "1" -or $env:SCANSTORY_TESTING -eq "1") {
        Fail "Production must not run with FLASK_DEBUG=1 or SCANSTORY_TESTING=1."
    }
}

Write-Host "Environment inventory check passed without printing secret values."
