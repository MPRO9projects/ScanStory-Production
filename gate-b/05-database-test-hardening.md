# Database Test Hardening

Added coverage:

- SQLite test DB guard.
- clean app context/session cleanup.
- relationship cleanup for Project -> ProjectPair.
- unique constraint check for `(project_id, pair_index)`.
- repeated fixture creation through isolated per-test DB.

The next data-model gate can add Workspace/Experience mapping fixtures without touching legacy production models first.

