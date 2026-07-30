---
title: Admin And Support Journey
tags:
  - scan-story/release-1
  - support
status: draft
---

# Admin And Support Journey

## Admin Jobs

- Inspect user, workspace, plan, entitlement, and billing state.
- View Experience status, trigger status, processing job history, and publication history.
- Diagnose scanner startup failures by browser/device/error code.
- Retry safe failed jobs with idempotency.
- Pause or archive abusive or broken Experiences.
- Export support-safe diagnostics without camera images or biometric data.

## Support Journey

```mermaid
flowchart TD
  A[Support ticket] --> B[Find workspace or public key]
  B --> C[Inspect Experience and trigger status]
  C --> D{Issue type}
  D --> E[Processing failure]
  D --> F[Scanner failure]
  D --> G[Billing or entitlement]
  D --> H[Abuse/privacy]
  E --> I[Retry or advise upload fix]
  F --> J[Use diagnostics and fallback events]
  G --> K[Correct plan state or explain limit]
  H --> L[Pause, audit, escalate]
```

## Boundaries

Support tools must not expose raw private uploads beyond role permission. Public scanner diagnostics must be sanitized.

