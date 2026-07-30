---
title: Processing And Publishing Model
tags:
  - scan-story/release-1
  - processing
status: draft
---

# Processing And Publishing Model

```mermaid
stateDiagram-v2
  [*] --> Draft
  Draft --> Uploading
  Uploading --> Uploaded
  Uploaded --> Validating
  Validating --> Optimizing
  Optimizing --> ExtractingFeatures
  ExtractingFeatures --> Testing
  Testing --> Ready
  Validating --> Failed
  Optimizing --> Failed
  ExtractingFeatures --> Failed
  Failed --> RetryScheduled
  RetryScheduled --> Retrying
  Retrying --> Validating
```

## Job Record Requirements

Each processing job records job ID, trigger ID, operation type, status, attempts, progress, timings, failure category, sanitized message, diagnostics, processing version, algorithm version, and idempotency key.

## Publishing Flow

```mermaid
flowchart LR
  A[Draft ready] --> B[Build manifest]
  B --> C[Validate entitlements]
  C --> D[Generate QR assets]
  D --> E[Freeze published version]
  E --> F[Atomic link switch]
  F --> G[Emit publication event]
  G --> H[Public scanner serves new version]
```

No unmanaged daemon threads. Durable queues own processing.

