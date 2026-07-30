# Authentication Baseline

Covered:

- registration page
- valid registration
- duplicate email
- email verification success
- OTP creation
- login success
- login failure
- logout
- expired trial login behavior

External services are mocked:

- reCAPTCHA bypassed only in tests
- SMTP captured in memory

Still partial:

- password reset flow
- admin password reset email flow
- brute-force throttling beyond current login behavior

