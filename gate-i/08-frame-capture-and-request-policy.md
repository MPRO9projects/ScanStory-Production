# Frame Capture And Request Policy

Recognition requests are bounded by:

- One in-flight request at a time.
- Minimum request interval per runtime mode.
- Request timeout per runtime mode.
- Stale response rejection by request id.

This prevents browser-side request pileups during poor network or low-power device conditions.

