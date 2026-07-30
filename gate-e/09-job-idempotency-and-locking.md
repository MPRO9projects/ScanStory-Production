# Job Idempotency And Locking

Every job has an idempotency key scoped to Workspace.

Duplicate requests reuse the existing job.

Local claiming records worker ID, claimed time, and lease expiry. Expired claimed/running jobs become claimable again.

SQLite local locking is a development foundation, not the final production queue architecture.
