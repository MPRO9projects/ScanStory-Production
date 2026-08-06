param(
  [string]$PythonPath = ".\venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $PythonPath)) {
  Write-Error "Python executable not found: $PythonPath"
  exit 1
}

if ($env:SCANSTORY_QUEUE_MODE -ne "rq") {
  Write-Error "Set SCANSTORY_QUEUE_MODE=rq before starting the real RQ worker."
  exit 1
}

if (-not $env:REDIS_URL) {
  Write-Error "REDIS_URL is required. The value will not be printed."
  exit 1
}

$queueName = if ($env:RQ_QUEUE_NAME) { $env:RQ_QUEUE_NAME } else { "scanstory-processing" }
Write-Host "Starting RQ worker for queue: $queueName"
& $PythonPath rq_worker.py
exit $LASTEXITCODE
