# Monitoring and Alerting

## Health Contracts

`GET /healthz`

- HTTP 200 when the process is alive.
- Liveness only.
- `Cache-Control: no-store`.

`GET /ready`

- HTTP 200 when the database is ready, the queue is usable, and — in `rq` mode —
  at least one live RQ worker is attached to the processing queue.
- HTTP 503 when the database is unavailable, the queue is unavailable, or
  `rq` mode is configured with **zero usable workers**.
- `Cache-Control: no-store`.
- Must not expose exception text, database URLs, paths, or credentials.
- Response shape: `{"status": "ready"|"not_ready", "checks": {...}}`. In `rq`
  mode `checks` carries `queue`, `workers` (`ok` / `unavailable`) and
  `usable_worker_count` (a count only — never worker names, hostnames or job
  payloads).

### Worker requirement (V1.1 P1-3)

**Production MUST run the RQ worker process, and MUST monitor it.** A reachable
Redis with no worker attached accepts uploads and processes none; before this
change `/ready` reported 200 in exactly that state.

- Start the worker alongside the web process:
  `python -m flask --app app` is *not* enough — run `python rq_worker.py`
  (same `REDIS_URL`, `SCANSTORY_QUEUE_MODE=rq`, `RQ_QUEUE_NAME`) as its own
  supervised service, with automatic restart.
- Worker liveness is derived from RQ's own heartbeat. A worker whose last
  heartbeat is older than `RQ_WORKER_STALE_AFTER_SECONDS` (default 420s) is not
  counted as usable.
- `checks.workers == "not_applicable"` means a non-`rq` queue mode
  (`fake`/`inline`). That is a supported non-production mode only; the runtime
  config validation already refuses to boot a production-flagged deployment in
  any mode other than `rq`.

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
- Razorpay webhook rejection-rate alert (`missing_signature`/`invalid_signature`
  spikes can indicate a misconfigured secret or a probing attempt).
- Razorpay webhook `failed` processing-status alert (`unknown_order`,
  `amount_mismatch`, `currency_mismatch`, `payment_id_conflict`).
- **RQ worker-count alert: alert immediately when `/ready` reports
  `checks.workers == "unavailable"` or `usable_worker_count == 0`.** This is the
  "queue accepts jobs, nothing runs them" condition.
- Worker process supervision alert (process exited / restart loop).

## Suggested Initial Thresholds

Tune with real traffic:

- `/healthz`: alert after 3 consecutive failures.
- `/ready`: alert after 2 consecutive failures.
- Scanner API p95 latency: warning at 1 second, critical at 3 seconds.
- Disk usage: warning at 75%, critical at 90%.
- Payment activation failures: alert immediately.
- Secret-looking log event: alert immediately.
- Webhook `secret_not_configured` rejection: alert immediately (means
  `RAZORPAY_WEBHOOK_SECRET` is missing in an environment expected to receive
  webhook deliveries).

## Webhook Inspection Commands (read-only)

Use these to inspect Razorpay webhook reconciliation state without querying
the database directly. None of the three mutate any table:

```powershell
python -m flask --app app webhook-events-status --limit 50
python -m flask --app app reconcile-order-webhooks <order_id>
python -m flask --app app webhook-replay-report
```

- `webhook-events-status` — recent `received`/`failed` events, useful for
  spotting a stuck or misbehaving webhook integration.
- `reconcile-order-webhooks <order_id>` — full webhook history tied to one
  `PaymentOrder`, useful when investigating a specific customer/order.
- `webhook-replay-report` — aggregate count of distinct events and observed
  replay/duplicate deliveries, useful for confirming Razorpay's retry volume
  is within expectation.

## Log Hygiene

Logs must not contain:

- Raw passwords or credentials.
- Razorpay key secret.
- Razorpay webhook secret (`RAZORPAY_WEBHOOK_SECRET`).
- Raw payment signatures.
- Raw `X-Razorpay-Signature` header values.
- Raw webhook request payload/body.
- Auth/session cookies.
- Private media filesystem paths.
- Full request bodies with files.
- Customer email inside payment order payload logs.
