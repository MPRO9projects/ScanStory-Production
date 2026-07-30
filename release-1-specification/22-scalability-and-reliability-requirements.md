---
title: Scalability And Reliability Requirements
tags:
  - scan-story/release-1
  - reliability
status: draft
---

# Scalability And Reliability Requirements

## Requirements

- Durable queues for validation, probing, transcoding, thumbnails, features, robustness testing, QR generation, publication, and analytics.
- Storage abstraction supports local development and future object storage.
- Processing jobs are idempotent and retryable.
- Public scanner uses immutable published manifests.
- Publication switch is atomic.
- Analytics ingestion is buffered.
- Processing failures do not take down dashboard or viewer.
- Scanner dependency failures route to fallback.
- Health checks cover web, queue, storage, database, recognition, and billing integrations.

## Storage Categories

Original uploads, optimized images, optimized videos, poster frames, feature files, QR assets, published manifests, temporary processing files, logs, and diagnostics.

## Revision 1 Reliability Rules

Decision status: Approved Release 1 rule.

- Heavy processing never runs inside normal viewer or creator web requests.
- Future 3D, AR, VR, AI, enterprise, or bulk features must not be bundled into the core image-video scanner unless the active Experience explicitly requires them.
- Candidate retrieval must prevent large Experiences from sequentially matching every Trigger.
- Modules must be independently disableable and rollbackable through feature flags or route isolation.
