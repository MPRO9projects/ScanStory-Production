# Unknowns And Required Information

## Required Before Optimization

- Actual production start command: determines whether debug server risk is real.
- Production CPU/RAM/swap and p95 latencies: required to separate code vs server limits.
- Real Lighthouse and scanner traces: required to quantify frontend/scanner bottlenecks.
- Database row counts and slow query log: required before query/index changes.
- Expected acceptable scanner recognition latency and accuracy.

## Required Before AWS Staging

- Full environment variable inventory with masked values.
- Database engine/version and size.
- Media/features/QR folder sizes.
- External SMTP/Razorpay/reCAPTCHA test credentials.
- Current DNS/TLS setup.

## Required Before AWS Production Migration

- Backup/restore process.
- Production traffic and concurrent scanner-user estimates.
- Upload volume and retention expectations.
- Cutover and rollback window.
- Monitoring/alerting requirements.

## Optional But Helpful

- Business plan tiers expected long term.
- Admin workflow preferences.
- Target mobile device classes.
- Expected video duration/resolution.

