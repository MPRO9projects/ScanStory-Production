# Migration Idempotency

Gate C checks existing mappings before creating records:

- Existing owner workspace membership prevents duplicate workspace creation.
- Existing `Experience.legacy_project_id` prevents duplicate experience creation.
- Existing `Trigger.legacy_project_pair_id` prevents duplicate trigger creation.
- Existing asset storage keys are reused per workspace.

Database unique constraints provide a second line of protection.
