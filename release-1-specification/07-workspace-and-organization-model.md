---
title: Workspace And Organization Model
tags:
  - scan-story/release-1
  - architecture
status: draft
---

# Workspace And Organization Model

Organizations own workspaces. Workspaces own Experiences, members, roles, assets, brand settings, billing context, analytics, and limits.

```mermaid
flowchart TD
  Org[Organization] --> Ws[Workspace]
  Ws --> Member[Workspace Members]
  Member --> Role[Role Assignments]
  Ws --> Exp[Experiences]
  Ws --> Assets[Assets]
  Ws --> Brand[Brand]
  Ws --> Billing[Subscription and Entitlements]
  Ws --> Audit[Audit Log]
```

## Roles

- Owner: full control.
- Workspace Admin: workspace configuration and members.
- Creator: draft creation and uploads.
- Reviewer: review and comment.
- Publisher: publish, pause, rollback.
- Analyst: analytics access.
- Billing Admin: plan, invoices, payment.
- Viewer: public or restricted viewing.

## Release 1 UI Simplification

The interface may expose fewer role presets at first, but the data model should support the full role set.

