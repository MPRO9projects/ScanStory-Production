---
title: Current Code Compatibility
tags: [scan-story/release-1, readiness/code]
status: draft
---

# Current Code Compatibility

## Current Application Shape

Current code is a Flask/Jinja monolith. `models.py` defines `User`, `Admin`, `SubscriptionPlan`, `TrialDetails`, `PaymentOrder`, `OTPCode`, `Project`, `ProjectPair`, `ScanLog`, activities, and system config. `app.py` owns auth, OTP, SMTP, Razorpay, upload, QR generation, background threads, scanner pages, `/detect_init`, `/detect_track`, admin routes, and file serving.

## Compatibility Map

| Target Entity | Current Reuse | Required Change | Risk |
|---|---|---|---|
| Organization | None | New table | Medium |
| Workspace | None | New table and ownership layer | High |
| Workspace Member | `User` | Membership table and role assignment | High |
| Billing Account | `User.subscription_*`, `PaymentOrder` | Workspace billing wrapper | High |
| Experience | `Project` | Additive mapping or new table | High |
| Experience Version | None | New immutable version table | High |
| Trigger | `ProjectPair` | Additive fields/new table | Medium |
| Asset | image/video files and pair columns | Asset table and storage abstraction | High |
| Recognition Artifact | `.npz` file path convention | Artifact table with algorithm version | Medium |
| Processing Job | in-process daemon threads | Durable job table/queue | High |
| Published Version | `Project.scanner_url`, QR fields | Publish state/version pointer | High |
| Public Experience Key | `project_id` URL | New stable key while preserving legacy route | High |
| Public Trigger Key | None | Optional new key | Medium |
| Experience Session | `ScanLog.scan_session_id` | Session table | Medium |
| Experience View | `ScanLog` and `scans_used` | Separate usage record | High |
| Recognition Event | `ScanLog` partial | Event table | Medium |
| Entitlement | `SubscriptionPlan` limits | Configurable entitlement records | High |
| Contract Override | Admin edits only | Override table | Medium |

## Critical Compatibility Requirements

- Preserve `/scanner/<project_id>`, `/qr/<filename>`, `/image/<project_id>/<image_id>`, `/video/<project_id>/<image_id>`.
- Preserve `.npz` naming during transition: `{project_id}_{pair_index}.npz`.
- Preserve admin-owned project paths and routes.
- Keep legacy `/detect_init` and `/detect_track` response fields until scanner JS is versioned.

Code compatibility score: **64/100**.

