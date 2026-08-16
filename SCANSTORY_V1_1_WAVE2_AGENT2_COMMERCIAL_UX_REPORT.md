# ScanStory V1.1 Wave 2 Agent 2 Commercial UX Report

## 1. Scope

Agent 2 updated customer/admin presentation only for V1.1 commercial UX. No scanner runtime, scanner recognition, billing fulfillment, payment activation, storage accounting, backend schema, entitlement calculation or upload-processing behavior was changed.

## 2. Worktree And Branch

- Worktree: `F:\ScanStory-main\ScanStory-v1.1-agent2`
- Branch: `agent/v1.1-experience-ux`
- Synced from `develop/scanstory-v1.1` before edits.

## 3. Backend Contract Observed

Existing backend fields and helpers used:

- `User.account_type`: `INDIVIDUAL`, `BUSINESS_VENDOR`
- `Project.experience_type`: `image_video`, `direct_qr`
- `Project.playback_mode`: `tracked_overlay`, `detect_once`, `direct`
- `SubscriptionPlan.total_project_limit`
- `SubscriptionPlan.total_scan_limit`
- `SubscriptionPlan.max_pairs_per_project`
- `purchased_project_capacity(user)`
- `purchased_scan_capacity(user)`
- `project_capacity_summary(user)`

## 4. Backend Contract Not Present

The current `SubscriptionPlan` model does not expose:

- plan family
- base storage quota
- storage/media policy fields
- per-plan experience entitlement fields
- lifecycle/revision status fields

The UI marks these as `Backend pending` and does not create fake inputs or hidden fields for them.

## 5. Account Type Presentation

Pricing now presents both locked account families:

- Individual
- Business / Vendor

Plan cards are not filtered by family because the backend plan-family field is not available in this worktree.

## 6. Experience Terminology

Customer/admin surfaces use only:

- Direct QR
- Detect Once
- Tracked Overlay

The UI does not call Tracked Overlay `Object Tracking`.

## 7. Plan Presentation

Pricing cards still show the existing backed plan data:

- price
- validity/duration
- project limit
- scan limit
- pairs per project
- feature list

Additional V1.1 policy rows are read-only and clearly mark unavailable backend contract fields as pending.

## 8. Admin Plan Editor

Admin Add/Edit Plan pages now explain that only existing `SubscriptionPlan` fields are editable. Missing plan-family, storage/media, experience entitlement and lifecycle/revision fields are not rendered as form controls.

## 9. Entitlement Summary

Added `user_entitlement_summary(user)` as presentation glue in `app.py`.

It reads existing backend values and ledger helpers. It does not enforce limits or mutate billing state.

## 10. Customer Surfaces Updated

- Dashboard: effective entitlement summary
- Profile: effective entitlement summary
- Pricing/Subscribe: account-family, experience, grandfathering and plan-policy presentation

## 11. Admin Surfaces Updated

- Admin Plans
- Admin Add Plan
- Admin Edit Plan
- Admin User Details
- Admin User Dashboard Context

## 12. Grandfathering Copy

Copy now states existing projects are retained and that new creation/scanning can be restricted when usage exceeds the effective allowance.

## 13. Upgrade/Downgrade Copy

Pricing now states:

- upgrades take effect after confirmed payment
- downgrades are scheduled for the next plan-term boundary
- downgrades do not delete existing projects

## 14. Creator Flow

The creator flow was not redesigned. Direct QR, Detect Once and Tracked Overlay submission behavior remains backed by existing tests.

## 15. Payment And Entitlement Safety

No changes were made to:

- Razorpay order creation
- payment verification
- webhook handling
- payment activation
- refund logic
- entitlement ledger write logic
- quota enforcement

## 16. Storage Accounting

No storage accounting or upload-size policy calculations were added. Storage/media policy remains backend-pending in presentation.

## 17. Tests Added

Added focused assertions in `tests/integration/test_v1_agent2_admin_parity.py` for:

- account-family presentation
- locked experience terminology
- non-destructive upgrade/downgrade wording
- admin plan-policy pending state
- absence of fake unbacked plan inputs
- profile entitlement summary from ledger data
- admin user entitlement summary and over-capacity copy

## 18. Tests Run

- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\integration\test_v1_agent2_admin_parity.py -q`
- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\integration\test_admin_navigation_routing.py -q`
- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\gate_jr\test_marker_selection_upload.py -q`
- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\contracts\test_scanner_contract.py -q`
- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\integration\test_admin_panel_repair.py -q`
- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m py_compile app.py`
- `git diff --check`

## 19. Known Limitations

Plan-family filtering and per-plan media/experience entitlement gates still require Agent 1 backend fields. This package intentionally does not invent them.

## 20. Merge Recommendation

PASS for UX/admin presentation. Safe to merge after review if the current Agent 1 backend contract remains unchanged.
