param(
    [switch]$ConfirmProductionReadOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail($Message) {
    Write-Error $Message
    exit 1
}

if ($env:FLASK_ENV -eq "production" -and -not $ConfirmProductionReadOnly) {
    Fail "Refusing production execution without -ConfirmProductionReadOnly. This script is read-only but may connect to the configured DB."
}

if ($env:SCANSTORY_OPS_ALLOW_LOCAL_CHECKS -ne "1") {
    Fail "Set SCANSTORY_OPS_ALLOW_LOCAL_CHECKS=1 to confirm this read-only local check."
}

if (-not $env:FLASK_SECRET_KEY) {
    Fail "FLASK_SECRET_KEY must be set for Flask CLI import. Value will not be printed."
}

Write-Host "Running read-only Alembic state checks. No migrations will be applied."
$headsOutput = python -m flask --app app db heads
$headsOutput | ForEach-Object { Write-Host $_ }
python -m flask --app app db history
python -m flask --app app db current

$expectedHead = "ebeab1cf4ec9"
$headsText = ($headsOutput -join "`n")
if ($headsText -notmatch $expectedHead) {
    Fail "Expected migration head '$expectedHead' (razorpay webhook events) not found in 'flask db heads' output."
}
$headLines = $headsOutput | Where-Object { $_ -match "\(head\)" }
if ($headLines.Count -gt 1) {
    Fail "Multiple Alembic heads detected; expected exactly one head at '$expectedHead'."
}
Write-Host "Confirmed single head at expected revision '$expectedHead'."
Write-Host "Alembic state verification completed."
