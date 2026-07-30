# Source Change Detection

Gate F uses storage-key-derived source hashes plus algorithm and pipeline versions to build idempotency keys.

Detected dimensions:

- reference image changed
- video changed
- algorithm version changed
- QR destination
- QR style
- pipeline version

QR destination changes are not allowed through ordinary retry processing.
