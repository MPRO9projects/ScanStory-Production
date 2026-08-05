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
7. Confirm subscription activation.
8. Replay callback/verification and confirm idempotent behavior.
9. Attempt wrong-user verification and confirm rejection.
10. Fill capacity and confirm capacity-full rejection happens before order
    creation.
11. Pause capacity and confirm order creation is blocked.
12. Expire reservation and confirm stale checkout cannot activate incorrectly.
13. Run stale reservation reconciliation.
14. Confirm logs contain no secrets, emails in payment payload logs, raw
    signatures, auth cookies, or credentials.

## Not Yet Certified

Webhook behavior is not certified until the later webhook phase is implemented.
Do not represent webhook handling as production-ready before that phase passes
its own staging certification.

## Evidence to Record

- Release commit.
- Test order identifiers without secrets.
- Capacity before/after.
- Activation result.
- Replay result.
- Wrong-user rejection result.
- Log-hygiene review result.
