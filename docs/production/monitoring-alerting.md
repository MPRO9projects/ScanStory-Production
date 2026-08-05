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
- Razorpay webhook rejection-rate alert (`missing_signature`/`invalid_signature`
  spikes can indicate a misconfigured secret or a probing attempt).
- Razorpay webhook `failed` processing-status alert (`unknown_order`,
  `amount_mismatch`, `currency_mismatch`, `payment_id_conflict`).
- Queue monitoring is future until Redis/RQ exists.

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
