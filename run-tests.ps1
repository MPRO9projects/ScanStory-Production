param(
  [Parameter(Mandatory=$true)]
  [ValidateSet("fast", "full", "contracts", "security", "coverage", "repeatability")]
  [string]$Suite
)

$ErrorActionPreference = "Stop"

$ExpectedRoot = "F:\ScanStory-main\ScanStory-main"
$CurrentRoot = (Get-Location).Path
if ($CurrentRoot -ne $ExpectedRoot) {
  Write-Error "Run tests from $ExpectedRoot. Current: $CurrentRoot"
  exit 2
}

$env:SCANSTORY_TESTING = "1"
$env:FLASK_SECRET_KEY = "gate-b-runner-secret"
$env:RAZORPAY_KEY_ID = ""
$env:RAZORPAY_KEY_SECRET = ""
$env:RECAPTCHA_SITE_KEY = ""
$env:RECAPTCHA_SECRET_KEY = ""

if ($env:DATABASE_URL -and $env:DATABASE_URL -notlike "sqlite:///*") {
  Write-Error "Unsafe DATABASE_URL is set. Clear it before running tests."
  exit 2
}

function Invoke-Test {
  param([string[]]$Args)
  & python -m pytest @Args
  return $LASTEXITCODE
}

switch ($Suite) {
  "fast" { exit (Invoke-Test @("-m", "not slow and not cv")) }
  "full" { exit (Invoke-Test @()) }
  "contracts" { exit (Invoke-Test @("tests/contracts")) }
  "security" { exit (Invoke-Test @("tests/security")) }
  "coverage" { exit (Invoke-Test @("--cov=.", "--cov-report=term-missing", "--cov-report=html")) }
  "repeatability" {
    $runs = @(
      @("-m", "not slow and not cv"),
      @("tests/security", "tests/contracts"),
      @("-m", "not slow and not cv")
    )
    for ($i = 0; $i -lt $runs.Count; $i++) {
      Write-Host "Repeatability run $($i + 1)"
      & python -m pytest @($runs[$i])
      if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    exit 0
  }
}
