---
title: ScanStory V1.1 Wave 4 Agent 2 Vendor Ownership UX Report
branch: agent/v1.1-experience-ux
foundation_commit: 6af5e3a
synced_backend_commit: 9ee44cd
tags:
  - scanstory
  - v1.1
  - wave4
  - ownership
  - vendor
---

# ScanStory V1.1 Wave 4 Agent 2 Vendor / Ownership UX Report

## Summary

Finalized the Wave 4 ownership UX against the real Agent 1 backend contract now merged into `develop/scanstory-v1.1`.

This pass preserves the foundation presentation from `6af5e3a`: creator vs current owner, vendor/customer context, non-destructive pending-capacity copy, claims as review requests, coverage separation and Wave 3 storage language.

## Commits

- Starting foundation commit: `6af5e3a`
- Synced backend integration commit: `9ee44cd`
- Final ending commit: recorded by the reconciliation commit that updates this report.

## Files Changed

- `app.py`
- `templates/user/project_preview.html`
- `templates/user/ownership.html`
- `templates/admin/view_project.html`
- `templates/admin/ownership.html`
- `tests/integration/test_v1_agent2_admin_parity.py`
- `SCANSTORY_V1_1_WAVE4_AGENT2_VENDOR_OWNERSHIP_UX_REPORT.md`

## Actual Agent 1 Routes Consumed

User:

- `GET /ownership`
- `POST /projects/<id>/transfer`
- `POST /ownership/transfers/<id>/accept`
- `POST /ownership/transfers/<id>/reject`
- `POST /ownership/transfers/<id>/cancel`
- `POST /ownership/transfers/<id>/retry`
- `POST /projects/<id>/ownership-claim`
- `POST /ownership/claims/<id>/respond`
- `POST /ownership/claims/<id>/cancel`

Admin:

- `GET /admin/ownership`
- `POST /admin/ownership/claims/<id>/approve`
- `POST /admin/ownership/claims/<id>/reject`
- `POST /admin/ownership/transfers/<id>/dispute`
- `POST /admin/ownership/transfers/<id>/release-dispute`
- `POST /admin/ownership/transfers/<id>/cancel`
- `POST /admin/ownership/transfers/<id>/complete`

## Actual States Consumed

Transfer states:

- `PENDING_ACCEPTANCE`
- `PENDING_CAPACITY`
- `COMPLETED`
- `CANCELLED`
- `EXPIRED`
- `DISPUTED`

Claim states:

- `OPEN`
- `APPROVED_BY_VENDOR`
- `PENDING_ADMIN_REVIEW`
- `APPROVED_BY_ADMIN`
- `REJECTED`
- `CANCELLED`
- `TRANSFER_COMPLETED`

The UI also remains tolerant of the existing backend active claim state `VENDOR_NOTIFIED`.

## Transfer Initiation UX

The central user ownership page lists transferable projects and posts to `POST /projects/<id>/transfer`.

Supported fields are wired from `app.py`:

- `recipient_email`
- `retain_vendor_management`
- `reason`

Copy explains that the recipient must accept and pass backend capacity checks before ownership changes.

## Accept / Reject / Cancel / Retry UX

Incoming transfers show:

- `Accept handover` for `PENDING_ACCEPTANCE` via `/accept`
- `Decline` for `PENDING_ACCEPTANCE` via `/reject`
- `Retry capacity check` for `PENDING_CAPACITY` via `/retry`

Outgoing active transfers show `Withdraw` via `/cancel`.

Terminal transfers are not surfaced with user actions.

## PENDING_CAPACITY UX

Where backend metadata includes `capacity_block`, the UI distinguishes:

- storage capacity
- project-slot capacity
- both when both are blocked

The copy states ownership has not changed, the current owner remains current owner, media and QR remain intact, and retry uses the same handover after capacity is available.

## Claim Submission UX

Project preview now links to the central ownership page and exposes `POST /projects/<id>/ownership-claim` when a non-owner project manager can view the project and has no active claim already.

Supported field:

- `evidence_summary`

Copy states that submitting a claim does not transfer ownership.

## Vendor Response UX

The central ownership page wires claim response forms to `POST /ownership/claims/<id>/respond`.

Supported fields:

- `decision=accept`
- `decision=refuse`
- optional `note`

The UI states that accepting opens a governed handover and refusal moves the request to Admin review.

## Claim Cancellation UX

Claimant-owned active requests expose `POST /ownership/claims/<id>/cancel`.

`TRANSFER_COMPLETED` is shown truthfully as a completed handover, not as an available cancellation.

## Admin Ownership UX

`templates/admin/ownership.html` preserves the real Agent 1 admin routes and uses existing permissions:

- `admin.ownership.view`
- `admin.ownership.manage`

Manage-capable admins see state-aware actions only:

- pending transfers: complete, dispute, cancel
- disputed transfers: release dispute, cancel
- terminal transfers: no action
- active claims: approve, reject
- terminal claims: no decision

Both transfer and claim decisions include recorded reason fields. No fake controls were added.

## History / Audit Presentation

The admin ownership queue keeps the existing audit trail display from backend `metadata_json` and shows actor IDs, action, status, timestamp and reason where present.

No raw media paths, secrets, credentials or arbitrary filesystem values are exposed.

## Coverage Separation

Project preview and Admin project detail keep coverage/service state separate from ownership state. Claims never imply ownership completion unless the claim reaches `TRANSFER_COMPLETED`.

## Storage Capacity Presentation

Wave 3 storage rules remain intact: pending capacity never deletes media or QR, and storage responsibility changes only after the backend completes the transfer.

## Mobile / Accessibility

The central ownership page now uses deterministic dark styling, responsive forms, wrapped actions and mobile-friendly controls down to 320px. Mutation controls remain semantic `POST` forms with CSRF tokens and keyboard-visible focus states.

## Focused Tests / Results

Run:

```powershell
& "F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe" -m pytest tests\integration\test_v1_agent2_admin_parity.py -q
```

Result:

```text
45 passed
```

Run:

```powershell
git -c safe.directory="F:/ScanStory-main/ScanStory-v1.1-agent2" diff --check
```

Result: passed.

## Tests Deliberately Not Run

- Full pytest suite
- Full PostgreSQL certification
- Scanner lifecycle/recovery suites
- Scanner contract suite

This reconciliation was limited to focused Wave 4 UX and admin parity coverage.

## Known Limitations

- `POST /ownership/transfers/<id>/accept` and `/retry` share one Flask endpoint; the retry form renders the documented `/retry` path explicitly.
- The central ownership page is the main workflow surface. Project preview links there instead of duplicating every sensitive transfer action.
- Regular `admin` and `superadmin` both currently have `admin.ownership.view` and `admin.ownership.manage`; the template still gates controls through `admin_can()`.

## Merge Risk

Low to moderate. This pass depends on the Agent 1 route names and state constants merged at `9ee44cd`. It did not change schema, transfer services, claim services, capacity math, storage accounting, scanner, payment or entitlement behavior.

## Wave 3 Preservation

Wave 3 storage UX was preserved. Existing storage summary/add-on behavior remains covered by the same focused parity suite.

## Scanner / Viewer

Scanner recognition, ORB, homography, RANSAC, optical flow, thresholds, camera logic and scanner cadence were untouched.
