# Storage Key And Path Policy

Canonical future identifiers are logical keys, not absolute paths.

Example shape:

```text
workspaces/{workspace_key}/experiences/{experience_key}/triggers/{trigger_key}/original/reference-image.ext
```

Policy:

- reject path traversal
- reject absolute user-controlled paths
- normalize separators
- keep provider-independent keys
- keep secrets out of filenames
- preserve legacy paths through read-only compatibility
