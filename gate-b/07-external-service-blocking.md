# External Service Blocking

Blocked in tests:

- SMTP via `smtplib.SMTP` and `SMTP_SSL`
- backend HTTP through `requests.sessions.Session.request`
- Razorpay through absent keys and mock clients
- reCAPTCHA through explicit test seam

Local Flask test client is not blocked.

