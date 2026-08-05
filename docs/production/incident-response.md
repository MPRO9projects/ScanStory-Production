# Incident Response

Use this document for first response. Create a separate incident record for
timeline, evidence, owner, impact, and follow-up actions.

## Razorpay Payment Captured but Activation Missing

1. Confirm payment capture in Razorpay dashboard.
2. Locate internal order/payment records without printing secrets.
3. Check reservation state and capacity.
4. Check signature validation logs.
5. Run `flask --app app reconcile-order-webhooks <order_id>` (read-only) to
   see whether a webhook delivery for this order was ever received, and if
   so, its `processing_status`/`failure_code`.
6. Preserve evidence: order ID, payment ID, user ID, plan ID, reservation ID,
   timestamps, and current row statuses. Do not record key secrets, signatures,
   cookies, or full request bodies.
7. Do not manually activate until ownership, payment status, plan, amount,
   currency, and reservation state are verified.
8. Use only an approved repair path. The current package provides capacity
   and quota reconciliation CLIs, and read-only Razorpay webhook inspection
   CLIs (`webhook-events-status`, `reconcile-order-webhooks`,
   `webhook-replay-report`) — there is still no automatic refund flow.
9. Record whether capacity was reserved, released, expired, activated, or
   inconsistent.

## Razorpay Webhook Rejected or Not Processing

1. Check for `razorpay_webhook_rejected reason=secret_not_configured` in
   logs — means `RAZORPAY_WEBHOOK_SECRET` is unset/empty in this
   environment; the route fails closed and processes nothing until it is
   configured. Confirm presence via secret manager, never print the value.
2. Check for `razorpay_webhook_rejected reason=missing_signature` or
   `reason=invalid_signature` — means Razorpay's dashboard webhook secret and
   this environment's `RAZORPAY_WEBHOOK_SECRET` do not match, or a
   non-Razorpay caller is probing the endpoint.
3. Run `flask --app app webhook-events-status` (read-only) to see recent
   `received`/`failed` rows and their `failure_code`.
4. Do not treat a rejected/unprocessed webhook delivery as a payment failure
   by itself — confirm actual payment/activation state via `/verify-payment`
   history and the reservation/order rows before escalating; the browser
   verification path is the primary activation path and may have already
   succeeded independently.
5. Remediate by correcting `RAZORPAY_WEBHOOK_SECRET` (secret manager) or the
   Razorpay dashboard webhook configuration, then confirm the next delivery
   or a manual re-send (from the Razorpay dashboard) processes cleanly.

## Duplicate Callback, Verification, or Webhook Delivery

1. Confirm idempotency result (browser: verification idempotency; webhook:
   the `razorpay_webhook_events.idempotency_key` UNIQUE-index rejection, a
   database-level gate, not an in-memory check).
2. Verify user counters/subscription state changed only once.
3. Check for repeated external calls (browser retries, or Razorpay's own
   webhook retries — both are expected and safe).
4. For a webhook replay specifically, `flask --app app webhook-replay-report`
   (read-only) reports total observed replay/duplicate deliveries.
5. Record duplicate identifiers without secret material.

## Capacity Counter or Reservation Drift

1. Stop new affected plan sales if drift can oversell.
2. Run read-only reconciliation/reporting.
3. Compare reservations, activated subscriptions, and capacity counters.
4. Repair only through approved command/process.

## Expired Reservations

1. Run stale reservation reconciliation in dry-run/report mode.
2. Confirm no active checkout is still valid.
3. Apply repair only after approval.

## Database Unavailable

1. Confirm `/ready` status.
2. Check database network/connectivity and credentials via secret manager.
3. Do not print database URL.
4. Fail over or restore according to database operations policy.

## Disk Full

1. Stop uploads if write failures are occurring.
2. Confirm affected mount/path.
3. Preserve logs needed for investigation.
4. Expand storage or remove only approved temporary files.
5. Verify uploads and media serving after recovery.

## Media Missing

1. Identify affected project and media type.
2. Check database record and storage object/path.
3. Restore from media backup if missing.
4. Verify scanner and project preview.

## Scanner Latency Spike

1. Check scanner endpoint latency and error rate.
2. Check CPU/memory and OpenCV/static asset delivery.
3. Confirm no recent scanner algorithm change entered the release.
4. Scale vertically or reduce traffic according to operations policy.

## Brute-Force/Login-Rate Alert

1. Confirm source IP and route.
2. Check whether ProxyFix/proxy headers are trustworthy.
3. Block at edge if needed.
4. Review login/OTP logs for targeted accounts.

## Suspected Forwarded-Header Spoofing

1. Remove direct app-port exposure immediately.
2. Confirm reverse proxy overwrites forwarded headers.
3. Review logs for impossible client IP patterns.
4. Rotate secrets if auth/session exposure is suspected.

## Suspected Secret Exposure

1. Revoke/rotate affected secret.
2. Restart application processes.
3. Invalidate sessions if `FLASK_SECRET_KEY` was exposed.
4. Search logs for access attempts.
5. Record timeline and user impact.

## Suspended Media Still Visible from Browser Cache

1. Confirm server now returns blocked response for the suspended project.
2. Explain public media may remain visible from browser cache for up to one hour.
3. For faster revocation, rotate or remove the underlying media object and purge
   any external cache/CDN if present.
4. Long-term fix: signed URLs or immutable versioned media with revocation.
