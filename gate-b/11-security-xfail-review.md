# Security Xfail Review

Retained xfails are strict and include severity, flow, desired behavior, current behavior, and future gate.

Retained:

- missing registered security headers
- CSRF disabled globally
- upload signature validation absent
- OTP brute-force throttling absent

No unexpected passes are ignored because `xfail_strict = true`.

