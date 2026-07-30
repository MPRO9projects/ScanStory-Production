# Cancellation And Recovery

Cancellation applies to pending, ready, and retry-scheduled jobs for one Trigger.

Claimed/running crash recovery relies on Gate E lease expiry and idempotent reclaim.

Cancellation does not affect unrelated Triggers.
