# Test Results

Focused Gate C suite:

```text
python -m pytest tests/models tests/migrations tests/compatibility --quiet
20 passed, 10 warnings in 76.09s
```

Regression results are filled in the final report after mandatory commands complete.

Mandatory runner commands, invoked through `powershell -ExecutionPolicy Bypass -File` because direct script execution is blocked locally:

```text
.\run-tests.ps1 fast: passed
.\run-tests.ps1 contracts: passed
.\run-tests.ps1 security: passed
.\run-tests.ps1 full: passed
.\run-tests.ps1 full: passed again
```

Final counted full suite:

```text
python -m pytest --quiet
80 passed, 4 xfailed, 89 warnings in 145.38s
```
