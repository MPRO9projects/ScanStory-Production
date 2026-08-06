param(
  [string]$PythonPath = ".\venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"

if (-not $env:DATABASE_URL) {
  Write-Error "DATABASE_URL is required for PostgreSQL development checks."
  exit 1
}

if ($env:DATABASE_URL -notmatch "^postgresql(\+[^:]+)?://") {
  Write-Error "DATABASE_URL must use a postgresql:// or postgresql+driver:// URL for this check."
  exit 1
}

if ($env:DATABASE_URL -match "(?i)(prod|production)") {
  Write-Error "Refusing production-like DATABASE_URL. Use a disposable development database."
  exit 1
}

if (-not (Test-Path $PythonPath)) {
  Write-Error "Python executable not found: $PythonPath"
  exit 1
}

& $PythonPath -c "from urllib.parse import urlparse; import os; u=urlparse(os.environ['DATABASE_URL']); print('Database host: ' + (u.hostname or '<local>')); print('Database name: ' + (u.path.lstrip('/') or '<unknown>'))"
exit $LASTEXITCODE
