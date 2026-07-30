---
title: Data Model Proposal
tags:
  - scan-story/release-1
  - data-model
status: draft
---

# Data Model Proposal

```mermaid
erDiagram
  ORGANIZATION ||--o{ WORKSPACE : owns
  WORKSPACE ||--o{ WORKSPACE_MEMBER : has
  WORKSPACE ||--o{ EXPERIENCE : owns
  EXPERIENCE ||--o{ EXPERIENCE_VERSION : versions
  EXPERIENCE_VERSION ||--o{ TRIGGER : contains
  TRIGGER ||--|| ASSET : reference_image
  TRIGGER ||--|| ASSET : video
  TRIGGER ||--o{ RECOGNITION_ARTIFACT : generates
  EXPERIENCE_VERSION ||--|| PUBLIC_LINK : publishes
  WORKSPACE ||--|| SUBSCRIPTION : billed_by
  WORKSPACE ||--o{ USAGE_EVENT : records
```

## Core Tables

Organization, Workspace, WorkspaceMember, RoleAssignment, Subscription, Entitlement, Experience, ExperienceVersion, Trigger, Asset, RecognitionArtifact, ProcessingJob, PublicLink, QRAsset, ScannerSession, RecognitionEvent, UsageEvent, AuditLog.

## Current-To-Target Mapping

User becomes Workspace Member. Project becomes Experience. ProjectPair becomes Trigger. Image file becomes Reference Asset. Video file becomes Content Asset. `.npz` feature becomes Recognition Artifact. Project QR becomes Permanent Experience Link and QR. ScanLog becomes Experience Session plus Recognition Event.

