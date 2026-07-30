# Scanner API Contract

Machine-readable contract:

`gate-a/scanner-contract-baseline.json`

Covered endpoints:

- `/detect_init`
- `/detect_track`
- `/api/scanner/session/end`

The tests cover invalid/missing payloads, invalid Project, invalid frame, and scan counting once per successful scanner session.

Actual recognition accuracy is not claimed by these tests.

