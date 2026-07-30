# Rollback Design

`rollback_experience_to_version()` verifies flags, authorization, target Version ownership, immutable snapshot presence, and historical asset references.

Rollback atomically points the Experience at the selected previous Version and keeps the permanent QR destination unchanged.
