---
title: Implementation Phases
tags:
  - scan-story/release-1
  - roadmap
status: draft
---

# Implementation Phases

## Phase 1: Product Core

Workspace, roles, Experience model, trigger model, assets, entitlements, and current model mapping.

## Phase 2: Processing Core

Durable jobs, validation, media variants, marker quality, feature generation, retries, diagnostics.

## Phase 3: Publishing Core

Versioned manifests, stable public links, QR assets, preview, approval, atomic publish, rollback.

## Phase 4: Viewer Core

Capability detection, scanner modes, camera flow, recognition contract, overlay, fallback.

## Phase 5: Billing And Analytics

Plan enforcement, usage metering, creator analytics, operations dashboards, audit logs.

## Phase 6: Enterprise Foundations

Bulk placeholders, approval workflow, custom branding, custom domain, managed-service support.

## Revision 1 Gate Alignment

Decision status: Approved Release 1 rule.

Implementation planning must begin with Gate A: repository and regression protection. No data model, scanner contract, QR compatibility, billing, or migration work begins until Gate A covers auth, upload, QR, legacy scanner, detection, payment, and admin/user ownership smoke tests.
