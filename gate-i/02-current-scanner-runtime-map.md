# Current Scanner Runtime Map

- Legacy route `/scanner/<project_id>` remains the scanner entry point.
- Legacy recognition route `/detect_init` remains the recognition entry point.
- Runtime shell now loads `static/js/scanner-runtime.js` before the inline scanner script.
- Browser runtime owns state transitions, capability detection, runtime mode selection, request bounding, stale-response rejection, and viewer-safe error text.
- Server runtime mirror lives in `scanner_runtime.py` for testable policy and contract checks.

