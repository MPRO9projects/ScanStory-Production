---
title: Billing And Usage Metering
tags:
  - scan-story/release-1
  - billing
status: draft
---

# Billing And Usage Metering

The primary billable usage unit is **Experience View**: one viewer launch of an Experience.

Do not bill every camera frame, recognition request, or server call.

```mermaid
flowchart TD
  A[Viewer launches Experience] --> B[Create session]
  B --> C[Count Experience View]
  C --> D[Check workspace allowance]
  D --> E{Policy exceeded?}
  E -- no --> F[Continue normally]
  E -- soft limit --> G[Notify owner and allow]
  E -- hard policy --> H[Show controlled fallback or upgrade]
```

## Additional Metering

Active Experiences, triggers, storage, bandwidth, media-processing minutes, seats, future API usage, and future 3D usage.

## Billing Reliability

Payment operations require idempotency. Webhook-ready billing state must be separate from public scanner availability.

