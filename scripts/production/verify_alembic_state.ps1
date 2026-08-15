param(
    [switch]$ConfirmProductionReadOnly,
    [string]$Python = $env:SCANSTORY_PYTHON
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

if (-not $Python) {
    $Python = "python"
}

Write-Host "Running read-only Alembic state checks. No migrations will be applied."
$headsOutput = & $Python -m flask --app app db heads
$headsOutput | ForEach-Object { Write-Host $_ }
& $Python -m flask --app app db history
$currentOutput = & $Python -m flask --app app db current
$currentOutput | ForEach-Object { Write-Host $_ }

$headLines = $headsOutput | Where-Object { $_ -match "\(head\)" }
if ($headLines.Count -ne 1) {
    Fail "Expected exactly one Alembic application head; found $($headLines.Count)."
}
$appHead = (($headLines[0] -split "\s+")[0]).Trim()
if (-not $appHead) {
    Fail "Could not parse Alembic application head from 'flask db heads' output."
}

$currentLines = $currentOutput | Where-Object { $_ -match "^[0-9a-fA-F]+" }
if ($currentLines.Count -ne 1) {
    Fail "Expected exactly one current database Alembic revision; found $($currentLines.Count)."
}
$dbCurrent = (($currentLines[0] -split "\s+")[0]).Trim()
if ($dbCurrent -ne $appHead) {
    Fail "Database Alembic revision '$dbCurrent' does not match application head '$appHead'. Run/review migrations before deployment."
}

Write-Host "Confirmed database revision matches current application head '$appHead'."
Write-Host "Alembic state verification completed."
