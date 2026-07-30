# Concurrency And Locking

Duplicate Trigger and Experience orchestration requests reuse idempotency keys.

Two workers claim different ready jobs.

Expired leases can be reclaimed.

Stale output activation is rejected when source identity does not match the job idempotency key.
