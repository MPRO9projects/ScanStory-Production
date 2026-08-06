param(
  [string]$PythonPath = ".\venv\Scripts\python.exe",
  [switch]$ConfirmApply
)

$ErrorActionPreference = "Stop"

if (-not $ConfirmApply) {
  Write-Error "Refusing to apply migrations without -ConfirmApply."
  exit 1
}

if (-not $env:DATABASE_URL) {
  Write-Error "DATABASE_URL is required."
  exit 1
}

if ($env:DATABASE_URL -match "(?i)(prod|production)") {
  Write-Error "Refusing production-like DATABASE_URL."
  exit 1
}

if (-not (Test-Path $PythonPath)) {
  Write-Error "Python executable not found: $PythonPath"
  exit 1
}

& $PythonPath -m flask --app app db upgrade
exit $LASTEXITCODE
