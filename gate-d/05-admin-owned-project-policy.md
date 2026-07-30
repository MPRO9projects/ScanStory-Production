# Admin-Owned Project Policy

Admin ownership is represented by `Project.owner_admin_id` with no `owner_user_id`.

Admin-owned projects must be classified through an explicit CSV or JSON mapping file accepted by `migration_gate_c.py --ownership-map`.

Required mapping fields:

```text
legacy_project_id
resolution_type
target_workspace_id
customer_reference
resolved_by
resolution_note
```

No customer-specific mapping is embedded in source code.
