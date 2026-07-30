# Legacy ID Preservation

Gate C preserves existing `Project.id` and `ProjectPair.id` values.

Current QR links remain `/scanner/<project_id>`.

Current media routes continue to use:

- `/image/<project_id>/<pair_index>`
- `/video/<project_id>/<pair_index>`
- `/qr/<filename>`

No migration code rewrites QR filenames, scanner URLs, project IDs, pair IDs, or feature artifact names.
