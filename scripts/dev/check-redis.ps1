param(
  [string]$PythonPath = ".\venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"

if (-not $env:REDIS_URL) {
  Write-Error "REDIS_URL is not configured. Use a local value such as redis://127.0.0.1:6379/0."
  exit 1
}

if (-not (Test-Path $PythonPath)) {
  Write-Error "Python executable not found: $PythonPath"
  exit 1
}

& $PythonPath -c "from processing_queue import redis_ready_check; raise SystemExit(0 if redis_ready_check() else 1)"
if ($LASTEXITCODE -ne 0) {
  Write-Error "Redis readiness check failed."
  exit $LASTEXITCODE
}

Write-Host "Redis readiness check passed."
