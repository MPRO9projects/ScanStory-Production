# Filesystem Test Hardening

Added assertions that these roots stay under pytest temp root:

- user data
- admin data
- images
- videos
- features
- QR
- static uploads

Upload hardening tests cover uppercase extension, double extension, missing extension, path traversal filename, empty files, pair limit, mismatched counts, and QR generation fallback behavior.

