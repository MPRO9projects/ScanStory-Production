# ScanStory V1.1 Wave 1 Agent 2 Admin UI Report

## Baseline

- Worktree: `F:\ScanStory-main\ScanStory-v1.1-agent2`
- Branch: `agent/v1.1-experience-ux`
- Starting pre-sync commit: `15e4d28`
- Fast-forwarded to `develop/scanstory-v1.1` at `f9cec78c2cdc35744e325c106d4b0a94f9569889`
- Scope kept to Admin/UI templates and focused Admin UI tests.

## Navigation

- Replaced duplicated standalone Admin sidebar bodies with `templates/admin/_sidebar_links.html`.
- Shared sidebar covers Dashboard, Users, Projects, Content Reports, Scans, Plans, Subscriptions, Payments, Admin Management, Capacity, Operations, Settings, and Activity Logs using existing `admin_can(...)` permission checks.
- Existing `templates/admin/base.html` already had deterministic normal, hover, focus-visible, active/current, and disabled sidebar states; standalone pages now reuse the same route set and keep deterministic link colors.
- Large template deletions are duplicated sidebar markup removal, not page truncation.

## Plan Edit Safety

- Added a live-plan warning to `templates/admin/edit_plan.html`.
- Clarified that project/scan limits and pairs-per-project can affect current/future entitlements differently.
- Clarified plan deactivation copy: disabled for new subscribers, historical subscriptions are not deleted.

## Payment Privacy

- Removed the unrestricted raw provider payload block from `templates/admin/view_payment.html`.
- Retained curated operational fields already rendered on the page: payment/order identifiers, amount, currency, status, refund/reconciliation details, and webhook-history link.
- Current model has no `PaymentOrder.payment_details` column on this branch; the stale template raw-dump path was still removed.

## Admin AJAX Resilience

- Added `templates/admin/_admin_fetch_helper.html`.
- Applied it to Admin fetch call sites in:
  - `templates/admin/moderation.html`
  - `templates/admin/operations.html`
  - `templates/admin/view_payment.html`
- Non-JSON redirects/HTML errors now produce safe session-expired or CSRF-expired messages instead of parser-error UX.
- No backend decorator/content-negotiation changes were made.

## Operations Truthfulness

- Reworded RQ/Redis fields to separate configuration from reachability.
- Added explicit note that configured Redis/RQ does not prove worker-online status.
- Reworded SMTP fields to separate configuration from delivery verification.
- Added explicit note that configured SMTP does not prove email delivery.
- Did not fabricate worker counts, migration heads, backup state, storage/account capacity, or SMTP last-send health.

## Destructive Action UX

- Updated copy for Admin deactivate/delete, user block/unblock, plan deactivate/delete, and project suspend/restore/delete.
- Copy now distinguishes suspend/deactivate/block from permanent delete and clarifies that project suspension does not delete media/QR/payment history or refund anything.
- Existing refund copy remains full-refund only and backend-driven.

## Placeholder / Dead UI Cleanup

- Converted inactive Settings sections from inert forms to read-only `role="group"` regions.
- Kept the real trial settings form active.
- Removed dead scan-log delete confirmation JavaScript from `scans.html` and `user_scans.html`; scan-log delete buttons remain disabled/unavailable because no delete backend is exposed.

## Accessibility

- Sidebar partial uses real anchors, visible labels, `aria-current`, icon `aria-hidden`, and keyboard-focus styling inherited from page CSS.
- Read-only Settings groups use explicit labels.
- AJAX error text is user-safe and visible in existing status/message regions.

## Tests

- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\integration\test_v1_agent2_admin_parity.py tests\integration\test_admin_navigation_routing.py -q`
  - `56 passed`
- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\integration\test_super_admin_authorization.py tests\integration\test_admin_crud_hardening.py tests\gate_jr\test_v11_admin_refund_ux.py tests\security\test_admin_panel_repair_csrf.py -q`
  - `41 passed`
- Jinja parse smoke:
  - `parsed 21 templates`
- `git diff --check`
  - clean

## Backend Dependencies

- Backend content negotiation for Admin JSON endpoints remains a future hardening item if Agent 1 chooses to standardize JSON responses for expired sessions/CSRF failures.
- Operations still needs backend fields for worker-online count, migration head, backup status, disk/storage/account capacity, centralized rate-limit status, and SMTP delivery success before the UI can display those claims.
- Plan-family/versioning/grandfathering semantics remain Wave 2+ backend/product work.

## Merge Risk Against Agent 1

- Risk: LOW to MEDIUM.
- This patch changes Admin templates and one Admin UI test file only.
- No migrations, models, commercial entitlement architecture, storage accounting, payment activation, scanner runtime, viewer runtime, or backend route logic changed.
- Merge conflicts are possible only if Agent 1 edits the same Admin template areas.

## Git State

- No staging or push performed before this report was written.
- Final commit created after tests/report.
