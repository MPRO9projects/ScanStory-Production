# Rollback Rehearsal

Rollback command:

```powershell
python migration_gate_c.py rollback --database-url sqlite:///F:/ScanStory-main/ScanStory-main/gate-d-small.sqlite --allow-rehearsal-rollback --execute-rollback
```

The rollback utility requires an explicit database URL and `--allow-rehearsal-rollback`.

Rollback removed target/checkpoint rows only. After rollback, status showed:

- users: 10
- projects: 20
- project pairs: 50
- workspaces: 0
- mapped experiences: 0
- mapped triggers: 0

No media, QR, `.npz`, scan-log, payment, user, project, or pair data was removed.
