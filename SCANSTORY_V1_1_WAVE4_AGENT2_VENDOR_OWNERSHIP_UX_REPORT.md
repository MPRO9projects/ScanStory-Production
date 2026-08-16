---
title: ScanStory V1.1 Wave 4 Agent 2 Vendor Ownership UX Report
branch: agent/v1.1-experience-ux
starting_commit: 38e913c
tags:
  - scanstory
  - v1.1
  - wave4
  - ownership
  - vendor
---

# ScanStory V1.1 Wave 4 Agent 2 Vendor / Ownership UX Report

## Summary

Built the pre-backend/foundation UX only. No final transfer or claim action wiring was added because this codebase currently exposes service functions and read-only state, but no HTTP routes for initiating/accepting/cancelling transfers or submitting/responding to claims.

## Contract Consumed

Existing backend state only:

- `Project.created_by_user_id`
- `Project.current_owner_user_id`
- `Project.manager_vendor_user_id`
- `Project.beneficiary_user_id`
- `ProjectOwnershipTransfer.status`
- `ProjectOwnershipClaim.status`
- `project_ownership_context(project, viewer)`
- `project_coverage_summary(project)`
- label maps `PROJECT_TRANSFER_STATUS_LABELS` and `PROJECT_CLAIM_STATUS_LABELS`

## Files Changed

- `app.py`
- `templates/user/project_preview.html`
- `templates/admin/view_project.html`
- `tests/integration/test_v1_agent2_admin_parity.py`

## Ownership Presentation

- User project detail now distinguishes Creator from Current Owner when that distinction matters.
- Self-created projects stay simple and do not show duplicate vendor terminology.
- Admin project detail now shows Creator, Current Owner, Managing Vendor, and Customer / Beneficiary.

## Vendor-Managed UX

Vendor-managed project display uses existing `manager_vendor_user_id`; it does not imply the vendor still owns the project after transfer.

## Transfer UX

Read-only only. Existing transfer status renders through backend labels. No fake buttons or JavaScript ownership changes were added.

## Capacity UX

`PENDING_CAPACITY` copy now names project/storage capacity together, without calculating reasons locally.

## Storage Capacity UX

Copy preserves Wave 3 rules: media and QR remain intact; storage responsibility changes only when backend transfer completes.

## Claim UX

Claims are shown as review requests. Copy explicitly says a claim does not transfer ownership by itself.

## Admin Review UX

Admin project detail shows read-only transfer/claim context and recent claim rows. No approve/reject controls were added because no real route contract was available.

## History / Audit UX

Recent transfer state and up to five claim records are displayed through existing relationships. Raw JSON, paths, secrets and credentials are not exposed.

## Account Conversion UX

Deferred. No existing backend conversion route was found in this pass.

## Coverage Separation

Admin copy explicitly states coverage/service state is separate from ownership state.

## Mobile / Accessibility

Changes reuse existing responsive cards/tables and semantic labels. No new wide-only table was added to user mobile surfaces.

## Tests

Run:

```powershell
& "F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe" -m pytest tests\integration\test_v1_agent2_admin_parity.py -q
```

Result:

```text
40 passed
```

Run:

```powershell
git -c safe.directory="F:/ScanStory-main/ScanStory-v1.1-agent2" diff --check
```

Result: passed.

## Tests Not Run

- Full suite
- PostgreSQL certification
- Scanner recovery/lifecycle suites

## Backend Limitations / Deferred

- No real transfer initiation/accept/reject/cancel routes in current UI contract.
- No real claim submission/vendor-response/admin-decision routes wired here.
- Final Wave 4 reconciliation must happen after Agent 1 publishes any additional route/action contract.

## Wave 3 Preservation

Wave 3 storage UX was not rewritten. Existing storage summary/add-on behavior remains covered by the same focused parity suite.

## Scanner / Viewer

Scanner recognition, homography, optical flow, thresholds and camera logic were untouched.

## Merge Risk

Low for read-only presentation. Medium if Agent 1 later changes transfer/claim route/state names; this work deliberately avoided guessed actions so reconciliation should be small.
