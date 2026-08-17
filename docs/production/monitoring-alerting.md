# Monitoring and Alerting

## Health Contracts

`GET /healthz`

- HTTP 200 when the process is alive.
- Liveness only.
- Does not check database, Redis, RQ workers, SMTP, Razorpay, or storage.
- `Cache-Control: no-store`.
- Must not expose exception text, URLs, paths, or credentials.

`GET /ready`

- HTTP 200 when mandatory deployment dependencies are usable.
- HTTP 503 when any mandatory readiness component is unavailable.
- `Cache-Control: no-store`.
- Must not expose exception text, database URLs, Redis URLs, worker hostnames,
  file paths, payment keys, SMTP credentials, or stack traces.
- Response shape: `{"status": "ready"|"not_ready", "checks": {...}}`.

Readiness checks include:

- `database`: minimal `SELECT 1`.
- `queue`: `ok`, `fake`, `inline`, or `unavailable` according to effective queue mode.
- `workers`: `ok`, `unavailable`, or `not_applicable` where RQ worker awareness applies.
- `usable_worker_count`: count only; never worker names or hostnames.
- `configuration`: production-only safe config readiness label.
- `payments`: production-only Razorpay API/webhook config readiness label.
- `csp`: production-only CSP enforcement readiness label.

## Worker Requirement

Production must run the RQ worker process and must monitor it. A reachable Redis
with no worker attached accepts uploads and processes none.

- Start the worker alongside the web process with `python rq_worker.py`.
- Use the same `REDIS_URL`, `SCANSTORY_QUEUE_MODE=rq`, and `RQ_QUEUE_NAME` as the web process.
- Worker liveness is derived from RQ heartbeat data.
- A worker with heartbeat older than `RQ_WORKER_STALE_AFTER_SECONDS` (default
  420s) is not counted as usable.
- `checks.workers == "not_applicable"` means non-RQ queue mode and is supported
  only outside production; production startup rejects non-RQ queue modes.

## Recommended Probes

- External HTTPS probe for `/healthz`.
- Internal readiness probe for `/ready`.
- Application latency threshold alert.
- Consecutive-failure alerting.
- PostgreSQL connectivity alert.
- Redis connectivity alert.
- RQ worker-count alert.
- Job queue growth and stuck/retrying job alert.
- Disk/storage utilization alert.
- Application error-rate alert.
- SMTP failure alert.
- Payment activation failure alert.
- Failed payment webhook alert.
- Refund reconciliation attention-state alert.
- Storage reconciliation error alert.
- Transfer expiry/reconciliation command failure alert.
- Scanner endpoint latency alert.
- Brute-force/login-rate alert.
- CSP header missing or report-only-in-production alert.

## Suggested Initial Thresholds

Tune with real traffic:

- `/healthz`: alert after 3 consecutive failures.
- `/ready`: alert after 2 consecutive failures.
- `/ready` `checks.payments == "unavailable"`: alert immediately in production.
- `/ready` `checks.csp == "unavailable"`: alert immediately in production.
- `/ready` `checks.workers == "unavailable"` or `usable_worker_count == 0`:
  alert immediately in production.
- Scanner API p95 latency: warning at 1 second, critical at 3 seconds.
- Disk usage: warning at 75%, critical at 90%.
- Payment activation failures: alert immediately.
- Webhook `secret_not_configured` rejection: alert immediately.
- Webhook processing failure codes (`unknown_order`, `amount_mismatch`,
  `currency_mismatch`, `payment_id_conflict`): alert immediately.
- Refund reconciliation attention states: alert immediately.
- Secret-looking log event: alert immediately.

## Webhook Inspection Commands

Use these read-only commands to inspect Razorpay webhook reconciliation state
without querying the database directly:

```powershell
python -m flask --app app webhook-events-status --limit 50
python -m flask --app app reconcile-order-webhooks <order_id>
python -m flask --app app webhook-replay-report
```

- `webhook-events-status`: recent `received`/`failed` events.
- `reconcile-order-webhooks <order_id>`: webhook history tied to one `PaymentOrder`.
- `webhook-replay-report`: aggregate replay/duplicate-delivery volume.

## Scheduled Operations Signals

The deployment must monitor scheduled command completion and non-zero exits for:

- `reconcile-refunds`
- `reconcile-storage --json`
- `expire-ownership-transfers --apply`
- `expire-stale-reservations --apply`
- `recover-processing-jobs --apply`
- `reconcile-capacity-reservations`
- `cleanup-upload-sessions --apply`

## Log Hygiene

Logs must not contain:

- Raw passwords or credentials.
- `FLASK_SECRET_KEY`.
- Razorpay key secret.
- Razorpay webhook secret.
- Raw payment signatures.
- Raw `X-Razorpay-Signature` header values.
- Raw webhook request payload/body.
- SMTP password or provider auth failure details.
- Auth/session cookies.
- Private media filesystem paths.
- Full request bodies with files.
- Customer email inside payment order payload logs.
