# Test Environment

Test command root: `F:\ScanStory-main\ScanStory-main`.

Install:

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

Fast suite:

```powershell
python -m pytest -m "not slow and not cv"
```

Full automated suite:

```powershell
python -m pytest
```

Coverage:

```powershell
python -m pytest --cov=. --cov-report=term-missing --cov-report=html
```

Contract suite:

```powershell
python -m pytest tests/contracts
```

Security suite:

```powershell
python -m pytest tests/security
```

Performance baseline:

```powershell
python gate-a\generate_baselines.py
```

Isolation is controlled by `SCANSTORY_TESTING=1`, `TEST_DATABASE_URL`, and temporary data/admin/static-upload paths. See `.env.test.example`.

