# Performance Review

Processing modules are not imported by Flask app startup or scanner routes.

No processing runs during viewer scanner requests.

Storage hashing streams reads in chunks.

Worker concurrency is explicit and local.

Existing performance smoke remains in the full regression suite.
