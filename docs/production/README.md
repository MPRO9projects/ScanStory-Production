# ScanStory V1 Production Operations

This package defines the staging certification, deployment, rollback, backup,
monitoring, and incident-response process for ScanStory V1.

It is documentation and safe local verification only. It does not change
application behavior, database schema, payment logic, scanner logic, upload
logic, cache behavior, or runtime security settings.

## Documents

- [Staging Certification](staging-certification.md)
- [Deployment Runbook](deployment-runbook.md)
- [Database Migration Runbook](database-migration-runbook.md)
- [Rollback Runbook](rollback-runbook.md)
- [Backup and Restore Runbook](backup-restore-runbook.md)
- [Monitoring and Alerting](monitoring-alerting.md)
- [Razorpay Certification](razorpay-certification.md)
- [Security and Proxy Checklist](security-proxy-checklist.md)
- [Incident Response](incident-response.md)

## Safe Verification Scripts

Scripts live in `scripts/production/`. They are read-only by default, print no
secret values, and do not deploy, push, restart services, or run migrations.

- `verify_release_state.ps1`
- `verify_alembic_state.ps1`
- `smoke_health_ready.ps1`
- `scan_for_secret_patterns.ps1`
- `verify_required_env.ps1`

## Environment Inventory

Classifications:

- Required: must be provided before production start.
- Optional: useful but not mandatory for all environments.
- Secret: must be stored only in the approved secret manager.
- Production-only: needed only for production.
- Staging-only: needed only for staging.
- Safe default: a missing value has a safe local default.
- No safe default: the operator must choose a real value.

| Category | Variable | Classification | Notes |
| --- | --- | --- | --- |
| Flask/session | `FLASK_SECRET_KEY` | required, secret, no safe default | Must be unique per environment. |
| Flask/session | `SESSION_COOKIE_SECURE` | required in production, production-only, safe default for local only | Set to `1` behind HTTPS. |
| Flask/session | `SECURITY_HSTS_ENABLED` | optional, production-only, safe default | Enable only after HTTPS is verified. |
| Flask/session | `SECURITY_CSP_ENABLED` | optional, safe default | Controls CSP header emission. |
| Flask/session | `SECURITY_CSP_ENFORCE` | optional, production-only, safe default | Enforce only after browser certification. |
| Database | `DATABASE_URL` | required, secret, production-only, no safe default | Production relational database URL. |
| Database | `TEST_DATABASE_URL` | optional, staging/local, secret if remote | Disposable/local test DB only. |
| Razorpay | `RAZORPAY_KEY_ID` | required for payments, secret, production-only/staging-only, no safe default | Use test-mode value in staging. |
| Razorpay | `RAZORPAY_KEY_SECRET` | required for payments, secret, production-only/staging-only, no safe default | Never log. |
| SMTP | `SMTP_HOST` | required for email, secret if private, no safe default | Approved mail provider host. |
| SMTP | `SMTP_PORT` | required for email, no safe default | Use provider-approved TLS port. |
| SMTP | `SMTP_USER` | required for email, secret, no safe default | Secret-manager only. |
| SMTP | `SMTP_PASS` | required for email, secret, no safe default | Secret-manager only. |
| SMTP | `MAIL_FROM` | optional, no safe default | Defaults to `SMTP_USER` when unset. |
| Proxy | `TRUSTED_PROXY_COUNT` | optional/future, production-only, no safe default | Code currently uses `ProxyFix(x_for=1)`; deployment must match one trusted proxy. |
| Storage | `SCANSTORY_DATA_DIR` | required, production-only, no safe default | Uploaded user media and artifacts. |
| Storage | `SCANSTORY_ADMIN_DATA_DIR` | required, production-only, no safe default | Admin-owned media and artifacts. |
| Storage | `SCANSTORY_STATIC_UPLOADS_DIR` | optional, production-only, no safe default | Static uploaded assets. |
| Bootstrap Admin | `BOOTSTRAP_ADMIN_ENABLED` | staging-only or first-production bootstrap only, safe default off | Disable after initial setup. |
| Bootstrap Admin | `BOOTSTRAP_ADMIN_EMAIL` | bootstrap-only, secret-adjacent, no safe default | Do not leave enabled. |
| Bootstrap Admin | `BOOTSTRAP_ADMIN_PASSWORD` | bootstrap-only, secret, no safe default | Rotate/remove after bootstrap. |
| Application mode | `FLASK_ENV` | required, no safe default | Use staging/production as applicable. |
| Application mode | `FLASK_DEBUG` | optional, safe default off | Must not be enabled in production. |
| Application mode | `SCANSTORY_TESTING` | optional, safe default off | Must not be enabled in production. |
| Logging | `LOG_LEVEL` | future, not yet active | Do not rely on this until logging config reads it. |
| Logging | `STRUCTURED_LOGGING_ENABLED` | future, not yet active | Use when centralized logging is ready. |
| Future queue | `REDIS_URL` | future, secret, not yet active | Required before Redis/RQ rollout. |
| Future queue | `RQ_QUEUE_NAME` | future, optional, not yet active | Queue monitoring is future until Redis/RQ exists. |
| Future limiter | `RATE_LIMIT_REDIS_URL` | future, secret, not yet active | Required before horizontal scale/shared limiter. |
| Razorpay | `RAZORPAY_WEBHOOK_SECRET` | required for webhook reconciliation, secret, production-only, no safe default | Dedicated Razorpay webhook secret, separate from `RAZORPAY_KEY_SECRET` with no fallback between them. `POST /webhooks/razorpay` fails closed (rejects, processes nothing) when this is absent/empty. |

## Current Integrated Runtime Constants

These values are from the current hardened integration branch and are included
so staging checks do not certify the wrong behavior.

| Area | Current value |
| --- | --- |
| Proxy trust | `ProxyFix(x_for=1, x_proto=1, x_host=1)`; deploy behind exactly one trusted proxy hop. |
| Paid capacity default | `SCANSTORY_INITIAL_CAPACITY_LIMIT`, default `25`. |
| Capacity reservation TTL | `SCANSTORY_CAPACITY_RESERVATION_TTL_MINUTES`, default `30`. |
| Scanner init limit | `45` requests per `60` seconds per normalized client IP. |
| Scanner tracking limit | `240` requests per `60` seconds per normalized client IP. |
| Scanner session-end limit | `90` requests per `60` seconds per normalized client IP. |
| Upload limit | `8` upload starts per `3600` seconds per normalized client IP. |
| Login IP limit | `80` attempts per `900` seconds per normalized client IP. |
| Register IP limit | `30` attempts per `3600` seconds per normalized client IP. |
| Forgot-password IP limit | `30` attempts per `3600` seconds per normalized client IP. |
| Resend-OTP IP limit | `20` attempts per `3600` seconds per normalized client IP. |
| Public media cache | `public, max-age=3600`; suspended media may remain in a browser cache until expiry. |
| OpenCV static cache | `public, max-age=31536000, immutable`; service worker only intercepts `/static/js/opencv*`. |
| CSP image sources | self/data/blob plus `https://images.pexels.com`; no broad `https:` or `*` image source. |
| Rate limiter | Process-local memory only; not shared across workers and reset on process restart. |
| Razorpay webhook route | `POST /webhooks/razorpay`; unauthenticated/no-session, `@csrf.exempt`, not covered by `request_limiter`; authenticity is `X-Razorpay-Signature` HMAC-SHA256 verification against `RAZORPAY_WEBHOOK_SECRET` only. |
| Razorpay webhook supported events | `payment.captured` only; every other validly-signed event type is acknowledged with zero mutation. |

## Required Pre-Deployment Checklist

- Release commit is confirmed and recorded.
- Git status is clean.
- Artifact/version record is created.
- Dependencies are installed from the approved lock or requirements file.
- Writable storage paths exist and are owned by the application user.
- Relational database backup is complete and restoreable.
- Upload/media backup is complete and restoreable.
- Migration duplicate-preflight is complete.
- Rollback authority is identified: `[Rollback Authority Role]`.
- Maintenance window or deployment communication is complete.
- Current `/healthz` and `/ready` baseline is captured.

## Known Operational Gaps

- The rate limiter is process-local and not shared between Gunicorn workers.
  Redis/shared limiting is required before horizontal scale.
- Queue monitoring is future until Redis/RQ exists.
- Razorpay webhook reconciliation (`POST /webhooks/razorpay`, migration
  `ebeab1cf4ec9`) is merged and covered by mocked/simulated automated tests,
  but is not yet staging-certified: real Razorpay test-mode webhook delivery
  against a real public HTTPS staging endpoint has not been exercised. See
  `razorpay-certification.md`'s Webhook Staging Checks before treating it as
  production-ready.
- There is no automatic refund flow, and no refund/chargeback/settlement/
  subscription-renewal webhook event support — out of scope entirely.
- Public media has a one-hour browser-cache limitation after suspension.
