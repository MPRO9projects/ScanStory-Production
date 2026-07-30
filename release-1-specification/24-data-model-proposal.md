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
  WORKSPACE ||--|| BILLING_ACCOUNT : billed_by
  BILLING_ACCOUNT ||--o{ ENTITLEMENT : grants
  BILLING_ACCOUNT ||--o{ CONTRACT_OVERRIDE : overrides
  WORKSPACE ||--o{ USAGE_EVENT : records
```

## Core Tables

Organization, Workspace, WorkspaceMember, RoleAssignment, BillingAccount, Subscription, Entitlement, ContractOverride, Experience, ExperienceVersion, Trigger, Asset, RecognitionArtifact, ProcessingJob, PublicLink, QRAsset, ScannerSession, ExperienceView, RecognitionEvent, UsageRecord, AuditLog.

## Current-To-Target Mapping

User becomes Workspace Member. Project becomes Experience. ProjectPair becomes Trigger. Image file becomes Reference Asset. Video file becomes Content Asset. `.npz` feature becomes Recognition Artifact. Project QR becomes Permanent Experience Link and QR. ScanLog becomes Experience Session plus Recognition Event.

## Revision 1 Additive Model Rules

Decision status: Approved Release 1 rule.

No destructive rename in the first migration. New tables and nullable compatibility fields wrap existing `User`, `Project`, `ProjectPair`, `ScanLog`, payment, and subscription records. Legacy IDs remain queryable. Workspace is required; Organization is optional. BillingAccount belongs to Workspace. UsageRecord is append-only. RecognitionArtifact records algorithm version and may point to existing `.npz` files until regenerated safely.
