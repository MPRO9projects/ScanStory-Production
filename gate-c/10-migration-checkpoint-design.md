# Migration Checkpoint Design

`migration_checkpoints` records migration name, entity type, legacy ID, target ID, status, attempt count, errors, and completion time.

The unique checkpoint key is:

```text
migration_name + entity_type + legacy_id
```

This supports rerun diagnostics and partial failure inspection.
