# Feature Flag Design

Central flags live in `feature_flags.py`:

- `ENABLE_EXPERIENCE_CREATOR`
- `ENABLE_TRIGGER_MANAGEMENT`
- `ENABLE_PROCESSING_STATUS_UI`
- `ENABLE_EXPERIENCE_QR_ASSET`

All default to disabled. Route checks are server-side and return `404` when disabled. No legacy navigation was changed, so no public links appear by default.
