# Additive Schema Design

New tables are additive:

- `organizations`
- `workspaces`
- `workspace_members`
- `experiences`
- `experience_versions`
- `triggers`
- `assets`
- `trigger_assets`
- `recognition_artifacts`
- `processing_jobs`
- `migration_checkpoints`

No legacy table, column, primary key, route, media path, scanner contract, or billing flow was removed or renamed.

Legacy mapping columns are nullable so empty installations and partial migration states are valid.
