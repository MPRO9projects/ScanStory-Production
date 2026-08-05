# Razorpay Staging Certification

Use Razorpay test-mode credentials on an HTTPS staging endpoint. Do not use
production keys during staging certification.

## Webhook Reconciliation Overview

`POST /webhooks/razorpay` is a server-to-server reconciliation endpoint,
merged at migration `ebeab1cf4ec9`. It exists to activate a subscription even
when the browser never returns to `/verify-payment` (tab closed, network
drop, etc.), by having Razorpay itself notify the server once a payment
captures.

- **Unauthenticated, session-independent.** The route never calls
  `current_user()` or any login helper — there is no browser session to bind
  to a server-to-server delivery.
- **CSRF-exempt (`@csrf.exempt`), and safe specifically because of that.**
  CSRF protection defends cookie-authenticated browser requests; this route
  has no cookie/session to forge in the first place. Authenticity instead
  comes entirely from the `X-Razorpay-Signature` header, verified as
  HMAC-SHA256 over the raw request body via the Razorpay SDK's own
  `razorpay.Utility().verify_webhook_signature(...)` (which itself uses
  `hmac.compare_digest`, not a hand-rolled comparison).
- **`RAZORPAY_WEBHOOK_SECRET` is a dedicated secret, separate from
  `RAZORPAY_KEY_SECRET`.** Razorpay issues one secret per configured webhook
  (dashboard > Webhooks) that is unrelated to the API key pair used for
  `razorpay_client.order.create()`. There is no fallback from one to the
  other in either direction — `RAZORPAY_KEY_SECRET` authenticates
  server-to-Razorpay API calls; `RAZORPAY_WEBHOOK_SECRET` authenticates
  Razorpay-to-server webhook deliveries. They are different trust boundaries
  and mixing them would let anyone who learned one secret forge traffic that
  should only be provable with the other.
- **Fails closed.** If `RAZORPAY_WEBHOOK_SECRET` is absent/empty, the route
  returns `400 {"error": "webhook_not_configured"}` and processes nothing —
  it never falls back to "skip verification" or "accept unsigned."
  A missing/invalid `X-Razorpay-Signature` is rejected the same way
  (`missing_signature` / `invalid_signature`, both `400`).
- **`payment.captured` is the only event that activates anything.** The
  payload's `payload.payment.entity` carries the same `razorpay_order_id`,
  payment id/status/amount/currency that `/verify-payment` already uses to
  look up the stored `PaymentOrder` and call `activate_payment()`.
- **Every other validly-signed event type is acknowledged, not processed.**
  The route returns a stable `200 {"status": "ok"}` and performs zero
  payment/quota/capacity mutation. This includes `order.paid` (a redundant
  view of the same capture in this one-order-one-payment flow) and anything
  else Razorpay might send. There is no automatic refund handling, and no
  refund, chargeback, settlement, or subscription-renewal webhook event is
  supported at all — this is an explicit scope boundary, not a silent gap.
- **Replay protection is a database unique constraint, not an in-memory
  check.** Every delivery is inserted into `razorpay_webhook_events` first,
  keyed by a deterministic `idempotency_key`
  (`"{event_type}|{payment_id}|{order_id}"` for `payment.captured`,
  `"{event_type}|{payload_hash}"` for anything else). A genuine duplicate
  delivery fails that INSERT at the database level (`IntegrityError` on the
  UNIQUE index), and the handler bumps `attempt_count` and returns
  `{"status": "ok", "replay": true}` without calling `activate_payment()`
  again. This holds across Gunicorn worker processes, unlike an in-process
  dict/set.
- **Browser/webhook race safety comes from `activate_payment()` itself, not
  a lock.** Both `/verify-payment` and `/webhooks/razorpay` route through the
  exact same `activate_payment()` service, whose activation gate is a single
  atomic conditional `UPDATE payment_orders SET status='success', ... WHERE
  id=? AND status='pending'`. Only one caller can ever see `updated == 1` for
  a given order, so whichever path (browser first, webhook first, or both at
  once) arrives first wins and the other observes the already-activated
  state — activation happens exactly once regardless of delivery order. No
  in-process lock is used or needed; a per-process lock would not coordinate
  across separate Gunicorn workers anyway.
- **Webhook-supplied amount/currency are never trusted.** The route compares
  the webhook entity's amount/currency against the stored `PaymentOrder` row
  and rejects (`amount_mismatch` / `currency_mismatch`, non-activating) on
  disagreement — the same principle `/verify-payment` already applies to
  browser-supplied values.
- **Unknown external orders and expired/released reservations are rejected,
  not created or activated.** An order id with no matching `PaymentOrder`
  finalizes as `unknown_order` with zero mutation; a reservation that
  `activate_payment()` finds already `released`/`expired` is rejected as
  `RESERVATION_EXPIRED` the same way it is for the browser path.

## Webhook Inspection CLIs (read-only)

Added alongside the webhook route; none of these three commands write to
`razorpay_webhook_events`, `payment_orders`, or any other table — they only
query and print:

- `flask --app app webhook-events-status [--limit N]` — most recent
  `received`/`failed` webhook events (default 20 rows).
- `flask --app app reconcile-order-webhooks <order_id>` — webhook event
  history for one stored `PaymentOrder.order_id`.
- `flask --app app webhook-replay-report` — count of distinct webhook events
  recorded and total replay/duplicate deliveries observed.

## Required Checks

1. Configure HTTPS staging endpoint.
2. Configure Razorpay test-mode key ID and key secret via secret manager.
3. Create order from authenticated user.
4. Confirm capacity reservation is created before checkout completion.
5. Complete successful checkout.
6. Validate payment signature.
7. Confirm server-side stored plan, amount, currency, and capacity reservation
   are authoritative; browser-provided plan/amount values must not activate a
   different entitlement.
8. Replay callback/verification and confirm idempotent behavior.
9. Attempt wrong-user verification and confirm rejection.
10. Attempt amount/plan mismatch and confirm rejection or no entitlement
    escalation.
11. Fill capacity and confirm capacity-full rejection happens before order
    creation.
12. Pause capacity and confirm order creation is blocked.
13. Lower configured capacity below current active/reserved count and confirm
    existing active users are not deactivated or evicted.
14. Expire reservation and confirm stale checkout cannot activate incorrectly.
15. Replay successful verification and confirm it does not reset quotas, extend
    subscription end again, or consume a second capacity slot.
16. Run stale reservation reconciliation.
17. Run capacity reconciliation in dry-run/report mode.
18. Confirm logs contain no secrets, emails in payment payload logs, raw
    signatures, auth cookies, or credentials.

## Webhook Staging Checks

Configure `RAZORPAY_WEBHOOK_SECRET` for the staging webhook endpoint via the
secret manager (test-mode webhook, not the production webhook secret) before
running these. Each check must be run against `POST /webhooks/razorpay` on
the real HTTPS staging endpoint.

| # | Check | Expected Result |
| --- | --- | --- |
| W1 | Deliver with no `X-Razorpay-Signature` header | `400 missing_signature`; no row written to `razorpay_webhook_events`. |
| W2 | Deliver with an invalid/incorrect signature | `400 invalid_signature`; no row written. |
| W3 | Deliver a valid `payment.captured` event for a real pending order | `200`; subscription activates exactly once; matches `activate_payment()` result. |
| W4 | Redeliver the exact same `payment.captured` event (replay) | `200 {"status": "ok", "replay": true}`; `attempt_count` increments; no second activation, no quota/capacity change. |
| W5 | Browser completes `/verify-payment` first, then the webhook arrives for the same order | Webhook observes the order already `success`; single activation only. |
| W6 | Webhook arrives first, then the browser completes `/verify-payment` for the same order | Browser observes the order already `success`; single activation only. |
| W7 | Deliver `payment.captured` for an order id with no matching `PaymentOrder` | `200`, `failure_code=unknown_order`; zero mutation, no entitlement created. |
| W8 | Deliver `payment.captured` with a mismatched amount | `200`, `failure_code=amount_mismatch`; no activation. |
| W9 | Deliver `payment.captured` with a mismatched currency | `200`, `failure_code=currency_mismatch`; no activation. |
| W10 | Deliver `payment.captured` for an order whose reservation is `released`/`expired` | `activate_payment()` returns `RESERVATION_EXPIRED`; `failure_code` recorded; no activation. |
| W11 | Deliver a validly-signed, unsupported event type (e.g. `order.paid`) | `200`, `failure_code=unsupported_event_type`; zero mutation. |
| W12 | Review application logs for W1-W11 | No `RAZORPAY_WEBHOOK_SECRET` value, no raw `X-Razorpay-Signature` value, no raw request payload/body, and no customer email appear anywhere in logs. |

All checks above must be run via a real, publicly reachable HTTPS staging
endpoint and real (test-mode) Razorpay webhook delivery — not only via
mocked/simulated local requests — before webhook reconciliation is considered
staging-certified.

## Not Yet Certified

The webhook route, model, migration, and CLIs are merged and covered by
mocked/simulated automated tests (signature verification, event handling,
idempotency, and browser/webhook race behavior). **Real Razorpay webhook
delivery has not yet been exercised** against a real staging environment with
real (test-mode) Razorpay credentials and a real public HTTPS endpoint — the
Webhook Staging Checks above are still outstanding. Do not represent webhook
handling as production-ready, and do not skip this section's checks, until
that live-delivery staging run passes.

There is no automatic refund flow in this package. Refunds remain an operator
or later integration concern and must not be implied by the V1 checkout flow.
No refund, chargeback, settlement, or subscription-renewal webhook event is
supported — this is out of scope entirely, not a silent gap.

## Evidence to Record

- Release commit.
- Test order identifiers without secrets.
- Capacity before/after.
- Activation result.
- Replay result.
- Wrong-user rejection result.
- Log-hygiene review result.
- Webhook staging check results (W1-W12 above), including confirmation the
  deliveries came from real Razorpay test-mode webhook delivery to a real
  HTTPS endpoint, not only simulated/mocked requests.
