# Rollback Procedure

Baseline commit: `2227968`.

Documentation checkpoint: `501fff4`.

Gate A branch: `gate-a-regression-baseline`.

To inspect changed files:

```powershell
git diff --name-only release-1-foundation...gate-a-regression-baseline
```

To return to the pre-Gate-A branch without destructive reset:

```powershell
git switch release-1-foundation
```

To discard only generated test caches:

```powershell
Remove-Item -Recurse -Force .pytest_cache, htmlcov -ErrorAction SilentlyContinue
```

Real production DB and upload folders are not used by the tests.

