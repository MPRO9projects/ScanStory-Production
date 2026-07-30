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
  Optimizing --> Extracting
  Extracting --> RobustnessTesting
  RobustnessTesting --> Ready
  Validating --> Failed
  Optimizing --> Failed
  Extracting --> Failed
  RobustnessTesting --> Failed
  Failed --> RetryScheduled
  RetryScheduled --> Retrying
  Retrying --> Validating
  Failed --> Excluded
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

## Revision 1 Canonical Trigger Lifecycle

Decision status: Approved Release 1 rule.

Trigger lifecycle:

```text
Draft -> Uploading -> Validating -> Optimizing -> Extracting -> Robustness Testing -> Ready
```

Failure states:

```text
Failed -> Retry Scheduled -> Retrying
Failed -> Excluded
```

## QR Timing Rule

Decision status: Approved Release 1 rule.

Legacy Projects may already create QR at upload time. Release 1 target publishing creates or activates the permanent Experience public key at publication. The compatibility resolver must preserve existing QR behavior while the new publishing model atomically switches the public key to the current Published Version.
