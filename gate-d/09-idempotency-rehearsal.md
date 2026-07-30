# Idempotency Rehearsal

The apply command was rerun twice on the small database.

Both reruns created zero target rows and reported existing records:

- workspaces: 10
- experiences: 17
- triggers: 43

The medium and large datasets also reran with zero new target rows.
