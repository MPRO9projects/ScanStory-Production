# Go No-Go Checklist

Blocking no-go conditions:

- backup not verified
- ownership exceptions unresolved beyond approved threshold
- duplicate legacy mappings
- reconciliation mismatch outside known exceptions
- migration rerun creates duplicates
- scanner contract regression
- QR regression
- auth regression
- payment regression
- logs incomplete
- rollback not rehearsed
- real media altered
- legacy data modified unexpectedly

Acceptable warnings:

- known admin-owned projects awaiting explicit mapping
- known unknown-owner test/demo projects awaiting policy resolution
- missing media/artifact warnings for preserved legacy references
- SQLAlchemy `Query.get()` deprecation warnings

Gate D does not authorize real migration.
