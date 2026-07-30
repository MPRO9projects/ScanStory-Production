# Dry-Run Results

Primary dry-run command:

```powershell
python migration_gate_c.py dry-run --database-url sqlite:///F:/ScanStory-main/ScanStory-main/gate-d-small.sqlite
```

Result:

- proposed workspaces: 10
- proposed memberships: 10
- proposed experiences: 17
- proposed triggers: 43
- proposed assets: 86
- proposed recognition artifacts: 43
- skipped projects: 3
- skipped project pairs: 7
- failed/warning entries: 10

Zero-write proof: after dry-run, status still reported 0 workspaces, 0 mapped experiences, and 0 mapped triggers.
