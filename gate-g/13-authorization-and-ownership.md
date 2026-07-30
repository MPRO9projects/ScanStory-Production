# Authorization And Ownership

All routes require authenticated users when enabled.

Workspace roles:

- `owner`, `admin`, `creator`: manage.
- `reviewer`, `publisher`, `analyst`: read-only.

Cross-Workspace access returns `403`.
