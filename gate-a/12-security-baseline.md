# Security Baseline

Security register:

`gate-a/security-baseline-register.csv`

Covered:

- protected admin route redirects to admin login
- user cannot view another user's Project
- current xfail for security headers not registered
- current xfail for CSRF disabled globally
- current xfail for file-signature validation gap
- current xfail for OTP brute-force throttling gap

No security behavior was changed except adding test-only isolation config.

