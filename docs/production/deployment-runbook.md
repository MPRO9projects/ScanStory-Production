# Deployment Runbook

This sequence is intentionally explicit. Run one controlled environment at a
time. Do not run commands against real/shared databases from a local shell.

## Pre-Deploy

1. Freeze the release commit and record the full SHA.
2. Confirm `git status --short` is clean.
3. Capture relational database backup.
4. Capture uploaded media/storage backup separately from the database.
5. Confirm backup integrity and restore location.
6. Verify environment variables without printing values:
   - Flask/session: `FLASK_SECRET_KEY`, `SESSION_COOKIE_SECURE=1`, debug off.
   - Database: PostgreSQL `DATABASE_URL`; SQLite/non-PostgreSQL is rejected.
   - Queue: `SCANSTORY_QUEUE_MODE=rq`, `REDIS_URL`, `RQ_QUEUE_NAME`.
   - SMTP: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `MAIL_FROM`.
   - reCAPTCHA: site key and secret configured for protected submissions.
   - Razorpay: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`.
   - CSP: `SECURITY_CSP_ENABLED=1`, `SECURITY_CSP_ENFORCE=1`.
   - Storage: user/admin/static upload directories exist and are writable by the app process.
7. Put release files in place.
8. Install dependencies from the approved requirements file.
9. Create or verify the artifact/version record.
10. Verify writable paths for images, videos, feature artifacts, QR assets, and logs.
11. Run `flask db heads`.
12. Run `flask db history`.
13. Run `flask db current`.
14. Run migration duplicate-preflight checks.
15. Review offline SQL where supported.
16. Confirm the rollback package/reference and rollback authority.
17. Run migration only after explicit approval.

## Deploy

1. Restart the application process under the production WSGI/app server.
2. Start or restart the RQ worker service with the same `REDIS_URL`,
   `SCANSTORY_QUEUE_MODE=rq`, and `RQ_QUEUE_NAME` as the web process.
3. Supervise both web and worker processes with automatic restart.
4. Confirm reverse proxy/app server expectations:
   - HTTPS terminates before the app and sets the trusted forwarded headers
     expected by `ProxyFix(x_for=1, x_proto=1, x_host=1)`.
   - Static assets are served without caching scanner HTML/API responses.
   - Upload body limits match `MAX_CONTENT_LENGTH`,
     `MAX_REQUEST_BODY_BYTES`, and `SCANSTORY_RESUMABLE_CHUNK_MAX_BYTES`.
   - CSP, CSRF, secure cookies, webhook signature validation, and debug-off
     settings are active.
5. Verify `GET /healthz`.
6. Verify `GET /ready`. It must report `checks.workers == "ok"` and
   `usable_worker_count >= 1`. In production it must also report safe `ok`
   labels for configuration, payments, and CSP.

A 503 with `workers: "unavailable"` means the worker is not running or its
heartbeat is stale. A 503 with `payments: "unavailable"` or
`csp: "unavailable"` means required production configuration is missing or
explicitly disabled. Do not release traffic.

## Post-Deploy Smoke

At minimum verify:

- Home/public page.
- Registration/login.
- User dashboard.
- Project list.
- Project creation using controlled smoke data, where suitable.
- Scanner/public access.
- Ownership center.
- Admin login.
- Admin user and project views.
- Payments configuration visibility.
- Refund attention/reconciliation view.
- Coverage grant.
- Claim flow.
- Email/SMTP smoke.
- reCAPTCHA-protected submission.
- Redis/RQ processing path.
- Public media and Range response.
- Suspended project blocking.
- Razorpay test-mode order and activation in staging.
- `GET /healthz`.
- `GET /ready`.
- Logs contain no secrets, credentials, emails in payment payloads, raw
  signatures, auth cookies, or private media paths.

Release traffic only after health, readiness, login, admin, upload, scanner,
media, suspension, and Razorpay test-mode staging evidence are recorded for the
exact release commit.

## Required Long-Running Processes

| Process | Command | Required in production |
|---|---|---|
| Web application | behind gunicorn/waitress or equivalent, never `flask run` | Yes |
| RQ processing worker | `python rq_worker.py` | Yes - uploads queue but never process without it |

Both must be supervised with automatic restart, and both must read the same
`REDIS_URL`, `SCANSTORY_QUEUE_MODE=rq`, and `RQ_QUEUE_NAME`.

## Scheduled Maintenance Commands

These reconciliation/recovery commands are dry-run by default and must be
scheduled, not run only when someone notices a problem. Suggested cadence; tune
with real volume. Review the output of every run.

| Command | Cadence | Non-zero exit means |
|---|---|---|
| `flask reconcile-refunds` | hourly | a refund is stuck or an out-of-band provider refund is unresolved; investigate immediately |
| `flask reconcile-storage --json` | daily | ambiguous media ownership or a hard reconciliation error |
| `flask expire-ownership-transfers --apply` | daily | transfer expiry sweep failed |
| `flask expire-stale-reservations --apply` | every 15 min | reservation expiry sweep failed |
| `flask recover-processing-jobs --apply` | every 15 min | job recovery sweep failed |
| `flask reconcile-capacity-reservations` | daily | counter drift needing repair |
| `flask cleanup-upload-sessions --apply` | daily | upload-session cleanup failed |

Run each command first without `--apply` in a new environment and record output
before enabling the scheduled `--apply` form.

## Security Release Checklist

- HTTPS is required before public traffic.
- `SESSION_COOKIE_SECURE=1`, `HttpOnly`, and SameSite cookie settings are active.
- `Content-Security-Policy` is present in production, not only report-only.
- CSRF protection is active on browser mutations.
- reCAPTCHA is configured for protected forms and fails closed in production.
- Razorpay webhook HMAC validation is configured and rejects unsigned traffic.
- Flask debug/reloader are disabled.
- No default/test Admin credentials remain enabled.

## Deployment Stop Conditions

- Database migration preflight fails.
- Backups cannot be verified.
- `/healthz` or `/ready` fails after restart.
- `/ready` reports zero usable RQ workers (`checks.workers: "unavailable"`).
- `/ready` reports production configuration, payments, or CSP unavailable.
- Login, upload, scanner, or payment activation smoke test fails.
- Authorization regression is detected.
- Logs expose secrets or private data.
- Error rate exceeds the agreed threshold.

Escalate to the rollback authority when any stop condition is met.

## Rollback Triggers

Start rollback when any of these are confirmed and cannot be corrected inside
the agreed startup window:

- App/site unavailable.
- `/ready` failing after process and worker restart.
- Migration failure or unexpected migration head.
- Login broken.
- Project upload, scanner, or public media path broken.
- Payment order, verification, webhook, or refund severe regression.
- Security configuration failure such as missing CSP, insecure cookies, or debug enabled.
- Storage/media inaccessible.

Rollback uses the existing rollback runbook and previously captured database and
media backups. This document does not define or imply an automated rollback
system.
