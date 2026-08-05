# Staging Certification

Run this certification on an HTTPS staging environment that uses staging-safe
credentials and a staging database copy. Do not use production secrets or a
shared production database.

## Entry Criteria

- Staging release commit is recorded.
- Staging database is disposable or restored from an approved masked copy.
- Staging storage paths are writable and not shared with production.
- Razorpay test-mode credentials are configured.
- SMTP uses staging-safe recipients or provider sandbox mode.
- `/healthz` and `/ready` return expected contracts.
- Logs are accessible to the certification operator.

## Certification Matrix

| Area | Check | Expected Result |
| --- | --- | --- |
| Liveness | `GET /healthz` | HTTP 200, JSON liveness, `Cache-Control: no-store`. |
| Readiness | `GET /ready` | HTTP 200 when DB is ready, 503 when DB is unavailable, no secret text. |
| Auth | User login/logout | Works without leaking whether unrelated accounts exist. |
| Admin | Admin and Super Admin login | Authorized roles reach expected pages only. |
| Upload | Project image/video upload | Valid media creates project; invalid media is rejected before rows/quota. |
| Scanner | Public scanner load | Camera/scanner page loads and can reach detection endpoints. |
| Media | Public image/video/QR | Serves authorized media; Range request returns 206 where applicable. |
| Suspension | Suspended project | Scanner and media routes are blocked server-side immediately. |
| Cache | Public media | `public, max-age=3600`; note browser cache window after suspension. |
| Cache | Admin media | `private` cache only. |
| Payment | Razorpay test checkout | Order, signature validation, activation, and capacity reservation pass. |
| Webhook | `POST /webhooks/razorpay`, missing signature | `400 missing_signature`; no event row written. |
| Webhook | `POST /webhooks/razorpay`, invalid signature | `400 invalid_signature`; no event row written. |
| Webhook | Valid `payment.captured` delivery | `200`; subscription activates exactly once via `activate_payment()`. |
| Webhook | Duplicate/replay delivery of the same event | `200 {"replay": true}`; safe no-op, no second activation (DB unique-constraint gate on `idempotency_key`). |
| Webhook | Browser verification then webhook, same order | Single activation only; webhook observes already-`success` order. |
| Webhook | Webhook then browser verification, same order | Single activation only; browser observes already-`success` order. |
| Webhook | Unknown external order id | `200`, `failure_code=unknown_order`; zero mutation. |
| Webhook | Amount mismatch vs. stored `PaymentOrder` | `200`, `failure_code=amount_mismatch`; no activation. |
| Webhook | Currency mismatch vs. stored `PaymentOrder` | `200`, `failure_code=currency_mismatch`; no activation. |
| Webhook | Released/expired reservation | No activation; `RESERVATION_EXPIRED`/failure recorded. |
| Logging | Secret hygiene | No secrets, raw signatures, credentials, cookies, or full payment payloads. |
| Logging | Webhook secret hygiene | No `RAZORPAY_WEBHOOK_SECRET` value, no raw signature header value, no raw webhook payload, and no customer email in logs. |

See `razorpay-certification.md`'s "Webhook Staging Checks" section for the
full W1-W12 procedure; this matrix only summarizes the pass/fail contract.
Webhook certification requires real Razorpay test-mode webhook delivery
against a real public HTTPS endpoint — mocked/simulated requests alone do not
satisfy this row.

## Staging Sign-Off

Record:

- Release commit.
- Database migration head/current output.
- Health/readiness baseline.
- Smoke-test results.
- Razorpay test transaction identifiers without secrets.
- Webhook staging check results (see `razorpay-certification.md`), including
  confirmation deliveries were real Razorpay test-mode webhook calls to a
  real HTTPS endpoint, not only simulated/mocked requests.
- Open defects and accepted risks.
- Rollback authority approval.

Do not certify production if any P0 or data-integrity issue remains unresolved.
