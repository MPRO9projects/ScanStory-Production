# Email Workflow

SMTP helper: `send_email_smtp`, `app.py:445`.

Required environment variables:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASS`
- `MAIL_FROM` optional fallback to username

| Trigger | Sender | Recipient | Subject Purpose | Template/Body Source | Failure Behaviour |
|---|---|---|---|---|---|
| User registration | `MAIL_FROM` | registering user | Email verification OTP | `templates/user/email_verification.html` | registration continues with flash error after OTP creation |
| User forgot password | `MAIL_FROM` | user email | Password reset OTP | `templates/user/email_verification.html` | flash error and redirect back |
| Payment success | `MAIL_FROM` | paying user | Payment successful | `templates/user/payment_success_email.html` | logs print, payment still succeeds |
| Admin forgot password | `MAIL_FROM` | admin email | Admin reset OTP | `templates/admin/reset_password_email.html` referenced in code, file not found in template listing | flash/error path depends route |
| Contact form | `MAIL_FROM` | `connect@myscanstory.com` | Contact inquiry | inline HTML in `send_contact_email` | JSON error on exception |

No scan-related email or error-notification email workflow was found.

