param(
  [string]$PythonPath = ".\venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"

if (-not $env:DATABASE_URL) {
  Write-Error "DATABASE_URL is required."
  exit 1
}

if (-not (Test-Path $PythonPath)) {
  Write-Error "Python executable not found: $PythonPath"
  exit 1
}

& $PythonPath -m flask --app app db current
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $PythonPath -m flask --app app db heads
exit $LASTEXITCODE
