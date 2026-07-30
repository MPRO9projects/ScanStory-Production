# Owner Resolution Input Format

Owner resolution is CSV based and validated by `gate_e_inputs.py`.

Required fields:

- `legacy_project_id`
- `resolution_type`
- `target_workspace_public_key`
- `target_workspace_id`
- `customer_reference`
- `ownership_status`
- `resolved_by`
- `resolved_at`
- `resolution_note`
- `approval_status`
- `approved_by`

Allowed resolution types: `customer_workspace`, `managed_service_workspace`, `internal_demo_workspace`, `unresolved`, and `exclude_from_migration`.
