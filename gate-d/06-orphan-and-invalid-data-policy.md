# Orphan And Invalid Data Policy

- Project with missing User: skip and checkpoint failure.
- ProjectPair with missing mapped Project/Experience: skip and checkpoint failure.
- Pair with missing image/video path: migrate with warning when parent mapping is valid.
- Pair with missing `.npz`: migrate artifact representation with missing status.
- malformed path: migrate with warning unless unsafe path validation fails in a future gate.
- duplicate legacy mappings: block migration for affected entity through unique constraints.
- duplicate Workspace membership: blocked by unique constraint.
- invalid email: blocks affected user backfill.
- archived/inactive user: migrate unless product policy later excludes it.
- missing plan/payment relation: warning; billing behavior remains legacy.
- invalid scan relation: warning; scan logs are not migrated in Gate D.

One bad project must not stop valid projects from migrating.
