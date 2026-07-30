# Rollback Strategy

Gate C rollback is additive:

- Stop reading new tables.
- Inspect checkpoints for failures.
- Remove controlled test-created target records when running isolated tests.
- Restore from a database backup for any real environment rollback.

No destructive production rollback command is provided.

Before any non-test apply: create a database backup, run dry-run, run apply, run verify, and inspect checkpoint failures.
