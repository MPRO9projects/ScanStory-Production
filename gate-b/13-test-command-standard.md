# Test Command Standard

Supported runner commands:

```powershell
.\run-tests.ps1 fast
.\run-tests.ps1 full
.\run-tests.ps1 contracts
.\run-tests.ps1 security
.\run-tests.ps1 coverage
.\run-tests.ps1 repeatability
```

The runner checks current directory, sets test markers, rejects unsafe `DATABASE_URL`, and returns pytest exit codes.

If local PowerShell policy blocks direct script execution, use:

```powershell
powershell -ExecutionPolicy Bypass -File .\run-tests.ps1 <suite>
```
