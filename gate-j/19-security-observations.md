# Security Observations

Observed through automated tests:

- Draft video names did not appear publicly before publish.
- Same public key could not select arbitrary draft media.
- Public viewer session ids are non-sequential and stable only within a client session.
- Malformed/synthetic scanner frames returned safe no-match or validation responses.
- Existing four security xfails remain unchanged for the dedicated security gate.

Not observed on physical devices:

- Real camera frame non-storage
- Browser console payload leakage
- Mobile cache behavior

