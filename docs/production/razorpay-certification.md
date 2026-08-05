# Razorpay Staging Certification

Use Razorpay test-mode credentials on an HTTPS staging endpoint. Do not use
production keys during staging certification.

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

## Not Yet Certified

Webhook behavior is not certified until the later webhook phase is implemented.
Do not represent webhook handling as production-ready before that phase passes
its own staging certification.

There is no automatic refund flow in this package. Refunds remain an operator
or later integration concern and must not be implied by the V1 checkout flow.

## Evidence to Record

- Release commit.
- Test order identifiers without secrets.
- Capacity before/after.
- Activation result.
- Replay result.
- Wrong-user rejection result.
- Log-hygiene review result.
