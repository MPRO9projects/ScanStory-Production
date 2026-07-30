# Test Data And Fixtures

Fixtures live in `tests/conftest.py`.

Created deterministic fixtures:

- isolated Flask app
- test client
- isolated SQLite DB/session
- normal user
- expired user
- default trial plan
- admin
- Project
- ProjectPair
- multiple ProjectPairs
- ScanLog through tests
- OTP records
- temporary image/video/QR files
- temporary `.npz` feature artifact
- authenticated user/admin sessions
- captured email seam
- mocked Razorpay client in payment tests

No customer data is used.

