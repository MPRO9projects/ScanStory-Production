# Security Review

Controls:

- opaque public keys
- no Draft leakage in public route
- server-side Workspace authorization
- version ownership checks
- safe public errors
- no internal diagnostics in viewer responses
- idempotency key length check

Known security xfails remain from earlier gates: CSRF, security headers, upload signature validation, and OTP throttling.
