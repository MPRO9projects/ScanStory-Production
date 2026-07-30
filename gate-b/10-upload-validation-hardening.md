# Upload Validation Hardening

Added current-behavior tests for:

- uppercase extensions
- double extension preservation
- missing video extension default
- path traversal filename not used for storage
- empty files currently accepted when image standardization is mocked
- mismatched counts rejected
- configured pair limit blocks upload
- QR generation fallback path

Security gaps remain documented for signature/MIME/deceptive content validation.

