# Repeatability And Flake Review

Repeatability command:

```powershell
.\run-tests.ps1 repeatability
```

On this workstation, direct script execution is blocked by Windows PowerShell policy. The validated equivalent invocation was:

```powershell
powershell -ExecutionPolicy Bypass -File .\run-tests.ps1 repeatability
```

The command runs three consecutive suites in different order:

1. fast suite
2. security plus contracts
3. fast suite

Results are recorded in `gate-b/repeatability-results.csv`.

All three validation runs passed with zero real failures and zero unexpected external calls.
