# Ownership Resolution Policy

User-owned projects map automatically:

```text
Legacy User -> personal Workspace -> mapped Experience
```

Admin-created projects require explicit mapping input. The allowed resolution types are:

- `customer_owned`
- `managed_service`
- `internal_demo`
- `test_data`
- `unknown`

Unknown ownership is never silently assigned.

Authorized resolvers: product owner, release manager, operations lead, or an explicitly delegated admin migration operator.
