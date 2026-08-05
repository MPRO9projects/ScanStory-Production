param(
    [string]$ExpectedCommit = "",
    [switch]$AllowDirty
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail($Message) {
    Write-Error $Message
    exit 1
}

$branch = git branch --show-current
$commit = git rev-parse --short HEAD
$status = git status --short

Write-Host "Branch: $branch"
Write-Host "HEAD: $commit"

if ($ExpectedCommit -and ($commit -ne $ExpectedCommit -and (git rev-parse HEAD) -ne $ExpectedCommit)) {
    Fail "HEAD does not match expected commit."
}

if ($status -and -not $AllowDirty) {
    Write-Host "Git status:"
    $status | ForEach-Object { Write-Host $_ }
    Fail "Working tree is not clean. Use -AllowDirty only for local investigation."
}

Write-Host "Release state verification passed."
