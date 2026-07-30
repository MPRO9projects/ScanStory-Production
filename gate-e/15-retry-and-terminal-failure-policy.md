# Retry And Terminal Failure Policy

Retryable:

- temporary file lock
- transient database issue
- temporary subprocess failure
- worker interruption
- temporary storage failure

Terminal:

- invalid image
- corrupt video
- unsupported media
- missing required source
- path outside allowed root
- invalid public destination
- repeated artifact validation failure

Retries use `max_attempts`, `attempt_count`, and `available_at`.
