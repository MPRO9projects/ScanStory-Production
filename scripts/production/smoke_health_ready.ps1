param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl,
    [switch]$ConfirmProductionProbe
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail($Message) {
    Write-Error $Message
    exit 1
}

if ($BaseUrl -notmatch "^https://") {
    Fail "BaseUrl must be HTTPS for staging/production smoke checks."
}

if ($env:FLASK_ENV -eq "production" -and -not $ConfirmProductionProbe) {
    Fail "Refusing production probe without -ConfirmProductionProbe."
}

function Test-Endpoint($Path, $ExpectedStatus) {
    $url = $BaseUrl.TrimEnd("/") + $Path
    try {
        $response = Invoke-WebRequest -Uri $url -Method Get -UseBasicParsing -TimeoutSec 15
    } catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq $ExpectedStatus) {
            return
        }
        Fail "Probe failed for $Path"
    }
    if ([int]$response.StatusCode -ne $ExpectedStatus) {
        Fail "$Path returned $($response.StatusCode), expected $ExpectedStatus"
    }
    $cache = $response.Headers["Cache-Control"]
    if ($cache -notmatch "no-store") {
        Fail "$Path missing Cache-Control: no-store"
    }
    Write-Host "$Path OK ($ExpectedStatus)"
}

Test-Endpoint "/healthz" 200
Test-Endpoint "/ready" 200
Write-Host "Health/readiness smoke checks passed."
