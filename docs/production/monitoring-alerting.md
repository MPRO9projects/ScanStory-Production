# Monitoring and Alerting

## Health Contracts

`GET /healthz`

- HTTP 200 when the process is alive.
- Liveness only.
- `Cache-Control: no-store`.

`GET /ready`

- HTTP 200 when the database is ready.
- HTTP 503 when the database is unavailable.
- `Cache-Control: no-store`.
- Must not expose exception text, database URLs, paths, or credentials.

## Recommended Probes

- External HTTPS probe for `/healthz`.
- Internal readiness probe for `/ready`.
- Latency threshold alert.
- Consecutive-failure alerting.
- Database connectivity alert.
- Disk/storage utilization alert.
- Application error-rate alert.
- Payment activation failure alert.
- Reservation drift alert.
- Scanner endpoint latency alert.
- Brute-force/login-rate alert.
- Queue monitoring is future until Redis/RQ exists.

## Suggested Initial Thresholds

Tune with real traffic:

- `/healthz`: alert after 3 consecutive failures.
- `/ready`: alert after 2 consecutive failures.
- Scanner API p95 latency: warning at 1 second, critical at 3 seconds.
- Disk usage: warning at 75%, critical at 90%.
- Payment activation failures: alert immediately.
- Secret-looking log event: alert immediately.

## Log Hygiene

Logs must not contain:

- Raw passwords or credentials.
- Razorpay key secret.
- Raw payment signatures.
- Auth/session cookies.
- Private media filesystem paths.
- Full request bodies with files.
- Customer email inside payment order payload logs.
