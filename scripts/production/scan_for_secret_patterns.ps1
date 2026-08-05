param(
    [string]$Path = ".",
    [switch]$IncludeAllFiles
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$patterns = @(
    "(?i)(secret|password|token|api[_-]?key)\s*=\s*['""][^'""]{8,}['""]",
    "(?i)razorpay[_-]?key[_-]?secret\s*[:=]\s*['""][^'""]+['""]",
    "(?i)razorpay[_-]?webhook[_-]?secret\s*[:=]\s*['""][^'""]+['""]",
    "(?i)database_url\s*[:=]\s*['""][^'""]+['""]",
    "(?i)smtp_password\s*[:=]\s*['""][^'""]+['""]",
    "rzp_(live|test)_[A-Za-z0-9]{8,}"
)

$extensions = @("*.md", "*.ps1", "*.py", "*.txt", "*.yml", "*.yaml", "*.json")
$files = if ($IncludeAllFiles) {
    Get-ChildItem -Path $Path -Recurse -File
} else {
    foreach ($extension in $extensions) {
        Get-ChildItem -Path $Path -Recurse -File -Filter $extension
    }
}

$findings = @()
foreach ($file in $files) {
    $content = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction SilentlyContinue
    foreach ($pattern in $patterns) {
        if ($content -match $pattern) {
            $findings += $file.FullName
            break
        }
    }
}

if ($findings.Count -gt 0) {
    Write-Host "Potential secret patterns found:"
    $findings | Sort-Object -Unique | ForEach-Object { Write-Host $_ }
    exit 1
}

Write-Host "No secret patterns found by this local heuristic scan."
