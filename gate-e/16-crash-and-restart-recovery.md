# Crash And Restart Recovery

Claimed/running jobs carry a lease expiry.

If a worker exits before completion, another worker can reclaim the job after the lease expires.

Idempotency keys prevent duplicate canonical work.

Temporary files are used for atomic publication, so previous good artifacts remain when regeneration fails.
