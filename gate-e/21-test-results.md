# Test Results

Focused Gate E tests:

```text
python -m pytest tests/gate_e --quiet
17 passed, 2 warnings
```

Broader focused slice:

```text
python -m pytest tests/models tests/migrations tests/compatibility tests/gate_e --quiet
50 passed, 15 warnings
```

Final regression results are recorded in `FINAL-GATE-E-REPORT.md`.
