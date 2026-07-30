# Default Workspace Backfill

`backfill_default_workspaces()` inspects existing users and creates one personal workspace plus one owner membership per user lacking an active personal owner workspace.

The operation supports dry-run and rerun.

Workspace names are deterministic: `<display name or email prefix>'s Workspace`.

Invalid users are reported and checkpointed on apply.

The migration is not run at application startup.
