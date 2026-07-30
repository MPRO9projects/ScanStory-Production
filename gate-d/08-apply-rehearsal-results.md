# Apply Rehearsal Results

Primary apply command:

```powershell
python migration_gate_c.py apply --database-url sqlite:///F:/ScanStory-main/ScanStory-main/gate-d-small.sqlite
```

Result:

- created workspaces: 10
- created memberships: 10
- created experiences: 17
- created triggers: 43
- created assets: 86
- created recognition artifacts: 43
- skipped projects: 3
- skipped project pairs: 7
- checkpoint failures: 10

Failures were expected ownership/dependent-pair exceptions.
