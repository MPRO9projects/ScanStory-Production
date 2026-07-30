# Test Results

Focused Gate F:

```text
python -m pytest tests/gate_f --quiet
11 passed
```

Foundation slice:

```text
python -m pytest tests/models tests/migrations tests/compatibility tests/gate_e tests/gate_f --quiet
61 passed
```

Final regression results are recorded in the final report.
