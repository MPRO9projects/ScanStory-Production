# Job State Machine

Canonical states:

- pending
- ready
- claimed
- running
- succeeded
- failed_retryable
- retry_scheduled
- failed_terminal
- cancelled

Invalid transitions are rejected by `processing_jobs.transition_job()`.
