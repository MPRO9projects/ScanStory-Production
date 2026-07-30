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

