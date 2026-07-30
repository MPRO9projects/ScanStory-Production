# Test Isolation Review

Gate B adds guard assertions for:

- `SCANSTORY_TESTING=1`
- SQLite test DB only
- temp data roots
- temp admin data roots
- temp upload roots
- no real Razorpay keys
- no real SMTP
- blocked backend HTTP calls

Tests fail immediately if core storage roots point outside the pytest temp root.

