# Incident Response

Use this document for first response. Create a separate incident record for
timeline, evidence, owner, impact, and follow-up actions.

## Razorpay Payment Captured but Activation Missing

1. Confirm payment capture in Razorpay dashboard.
2. Locate internal order/payment records without printing secrets.
3. Check reservation state and capacity.
4. Check signature validation logs.
5. Preserve evidence: order ID, payment ID, user ID, plan ID, reservation ID,
   timestamps, and current row statuses. Do not record key secrets, signatures,
   cookies, or full request bodies.
6. Do not manually activate until ownership, payment status, plan, amount,
   currency, and reservation state are verified.
7. Use only an approved repair path. The current package provides capacity and
   quota reconciliation CLIs, but no Razorpay webhook or automatic refund flow.
8. Record whether capacity was reserved, released, expired, activated, or
   inconsistent.

## Duplicate Callback or Verification

1. Confirm idempotency result.
2. Verify user counters/subscription state changed only once.
3. Check for repeated external calls.
4. Record duplicate identifiers without secret material.

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
