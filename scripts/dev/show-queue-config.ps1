param(
  [string]$PythonPath = ".\venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $PythonPath)) {
  Write-Error "Python executable not found: $PythonPath"
  exit 1
}

& $PythonPath -c "from processing_queue import queue_config_summary; import json; print(json.dumps(queue_config_summary(), sort_keys=True))"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
