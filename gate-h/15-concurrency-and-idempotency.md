# Concurrency And Idempotency

Publication accepts an idempotency key and returns the same published Version for repeated matching requests.

The service uses one active `current_published_version_id` and atomic commits. Full database row locks are not added because SQLite test portability is required.
