# ScanStory v1.1 — Wave 5 (Agent 1): Admin & Commercial Completion

Branch: `agent/v1.1-platform-admin`
Worktree: `F:\ScanStory-main\ScanStory-v1.1-agent1`

---

## 1. Starting commit

`49bb5ee6c5a2a4356e4bbd304f31d03bf0584c5c` (pre-sync HEAD, clean).

`develop/scanstory-v1.1` was at `9ee44cdc5f2669440e3779123dc4b69cb4a132ba`, exactly one merge
commit ahead. The merge base equalled my HEAD, so the sync was a clean **fast-forward** —
**no conflicts**, nothing to resolve:

```
Updating 49bb5ee..9ee44cd
Fast-forward
 12 files changed, 394 insertions(+), 47 deletions(-)
```

Post-sync working HEAD before Wave 5 code: `9ee44cdc5f2669440e3779123dc4b69cb4a132ba`.

Wave 4 backend re-verified present in the synced tree:

| Expected artefact | Verified |
|---|---|
| migration `a9d3c7e1b502_ownership_claim_audit_metadata.py` | present, `down_revision = "f2b7d4e9c3a6"`, **nothing depends on it → it is the head** |
| `GET /ownership` | `app.py:6873` |
| `GET /admin/ownership` | `app.py:14765` |
| transfer routes | `/projects/<id>/transfer`, `/ownership/transfers/<id>/{accept,retry,reject,cancel}`, `/admin/ownership/transfers/<id>/<action>` |
| claim routes | `/projects/<id>/ownership-claim`, `/ownership/claims/<id>/{respond,cancel}`, `/admin/ownership/claims/<id>/{approve,reject}` |
| permissions `admin.ownership.view` / `admin.ownership.manage` | `ADMIN_ROLE_PERMISSIONS`, both `admin` and `superadmin` roles |
| `can_convert_to_individual(user)` | `app.py:2365`, guard-only, **no caller** (that was the Wave 5 gap) |

Agent 2's parallel work added **no migration**, so `a9d3c7e1b502` remained the head as expected.

## 2. Ending commit(s)

`fdf6712` — `feat(v1.1): complete admin commercial governance` (single Wave 5 commit).

## 3. Files changed

| File | Change |
|---|---|
| `app.py` | plan-admin rewrite, plan lifecycle enforcement, add-on type immutability, shared admin entitlement grant helper, project-capacity grant route, account-type conversion route, refund list endpoint, coverage/ownership context on admin project view, zero-clamp on entitlement revocation |
| `templates/admin/add_plan.html` | plan_family, lifecycle_status, base_storage_bytes, per-file media policy, experience-entitlement inputs |
| `templates/admin/edit_plan.html` | same fields, pre-populated, plus revision display |
| `templates/admin/plans.html` | per-plan lifecycle control; policy-contract copy corrected (no longer claims the fields are read-only) |
| `templates/admin/view_project.html` | Service Coverage panel; current-owner vs creator vs managing-vendor vs beneficiary |
| `templates/admin/view_user.html` | project-capacity grant/revoke form; account-type conversion form |
| `tests/integration/test_wave5_admin_commercial_completion.py` | **new**, 709 lines, 38 focused tests |
| `tests/integration/test_v1_agent2_admin_parity.py` | Wave 2 assertion "these plan fields must have no admin input" inverted — Wave 5 backs them with real, validated inputs |

Diff: 7 modified files, +812/−339, plus 1 new test file.

## 4. Audit of existing commercial/admin functionality

Read end-to-end before touching anything:

**Models** — `SubscriptionPlan` (Wave 2 policy columns: `plan_family`, `lifecycle_status`,
`plan_revision`, `max_image_bytes`, `max_video_bytes`, `max_video_duration_seconds`,
`max_image_dimension_px`, `max_image_pixels`, `base_storage_bytes`, `allow_direct_qr`,
`allow_detect_once`, `allow_tracked_overlay`, `@validates` on family and lifecycle,
`is_purchasable`, `policy_snapshot()`), `AddonCatalog`, `AddonPurchase`,
`EntitlementTransaction`, `PaymentOrder` (carries `plan_policy_snapshot_json`),
`PaymentRefund`, `ProjectServiceCoverage`, `User` commercial/account fields, `AdminActivity`.

**Resolver** — `entitlements.get_effective_entitlements(...)` is the single resolver; it sums
plan base + purchased ledger + admin-grant ledger independently. `user_entitlement_summary()`
reads it and never re-derives plan math.

**Admin surface** — `/admin/plans[/add|/<id>/edit|/<id>/delete|/<id>/toggle-status]`,
`/admin/addons[/create|/<id>/edit|/<id>/toggle]`, `/admin/subscriptions/*`,
`/admin/payments/*` + `/admin/api/{payments,addon-purchases,refunds}/*`,
`/admin/users/<id>[/toggle-block|/reset-password|/extend-trial|/add-scans|/grant-storage]`,
`/admin/scans/<id>/{update-limit,grant-extra,lock-scanner}`,
`/admin/ownership*`, `/admin/projects/<id>/service-coverage/grant`, `/admin/capacity`.

**Permissions** — `ADMIN_ROLE_PERMISSIONS` + `HIGH_IMPACT_PERMISSIONS` +
`require_admin_permission(...)` + `admin_has_permission(...)`. Existing codes already cover
every Wave 5 action: `superadmin.plans.manage`, `superadmin.addons.manage`,
`admin.users.manage`, `admin.payments.view`, `admin.payments.refund`,
`superadmin.capacity.manage`, `admin.ownership.view/.manage`.

**Lifecycle** — `apply_pending_plan_change_if_due()` hooked into `check_user_limits()`;
`materialize_plan_entitlements()` rebuilds materialised columns from plan + ledger, so
purchased and admin-granted entitlement survives upgrade, downgrade and lapse.

**Ownership** — `set_project_current_owner()` writes `current_owner_user_id` **and mirrors
`owner_user_id`**, backfilling `created_by_user_id` first; `project_current_owner_user_id()`,
`project_created_by_user_id()`, `project_user_access_filter()` all resolve current ownership
correctly. `accept_project_ownership_transfer()` moves storage responsibility in the same
transaction and touches no coverage row.

## 5. Gaps classified A / B / C / D / E

### A — closed in Wave 5

| # | Gap | Evidence |
|---|---|---|
| A1 | Admin plan forms wrote **none** of the Wave 2 commercial policy columns. `plans.html` displayed `plan_family` / lifecycle / revision / storage / media policy / experiences as live values while both routes passed `v11_experience_options` to the template and then ignored every one of those fields on POST. | `admin_add_plan` / `admin_edit_plan` field list vs `SubscriptionPlan` columns |
| A2 | No server-side rejection of nonsensical plan values. `plan_amount`, `offer_price`, `duration_value`, `trial_days`, `total_project_limit`, `total_scan_limit`, `display_order` all accepted negatives; invalid numbers were silently swallowed by bare `except ValueError: pass` in the edit route, so a bad field was ignored rather than reported. | `app.py` old 13598–13688 |
| A3 | `plan_revision` was documented on the model as "bumped by admin edits to live commercial policy" and **nothing ever bumped it**. | grep: only reads, no writes |
| A4 | `SubscriptionPlan.is_purchasable` was **dead code**. Every customer-facing plan listing filtered on `is_active` only, and `/create-razorpay-order` validated `plan.is_active` only — so a `DRAFT`, `CLOSED_FOR_NEW_PURCHASE` or `ARCHIVED` plan was fully purchasable. The entire Wave 2 lifecycle vocabulary was unenforced. | grep `is_purchasable` → 1 definition, 0 callers |
| A5 | Plan hard-delete guarded on `User.subscription_id` alone. A plan whose subscribers had since moved on could be deleted out from under the `PaymentOrder` rows referencing it by id, and under `User.pending_plan_id`. | old `admin_delete_plan` |
| A6 | `AddonCatalog.addon_type` was freely editable on an item with existing purchases. `_apply_refund_reconciliation()` re-reads `item.addon_type` at reversal time, so a type edit reverses the **wrong** entitlement on every historical purchase of that SKU. | `app.py:9942`+ vs `_addon_catalog_form_values` |
| A7 | No admin `PROJECT_CAPACITY` grant/revoke path at all (Wave 1 gave scans, Wave 3 gave storage). `EXTRA_SCANS` had no revoke path — the route hard-rejected `<= 0`. | route inventory |
| A8 | Negative ledger deltas could drive `subscribed_scan_limit` / `subscribed_project_limit` **negative**, which `_limit_reached()` reads as unlimited. | `_apply_entitlement_transaction` |
| A9 | No account-type conversion route. `can_convert_to_individual()` existed with zero callers, so vendor capability could only be changed by hand-editing the database. | grep: 0 callers |
| A10 | No way to enumerate refunds. A refund whose provider call succeeded but whose reconciliation ended `FAILED` / `MANUAL_REVIEW_REQUIRED` was reachable only by already knowing its id. | `/admin/api/refunds/<id>` was the only read |
| A11 | Admin project page had no coverage inspection: no source, window, status, grantor or renewal eligibility — despite `project_coverage_summary()` already existing. | `admin_view_project` render args |

### B — already complete, deliberately untouched

- **Add-on catalogue CRUD & validation.** `_addon_catalog_form_values` already validates type
  membership, resolves the effect through the *same* `_addon_effect()` the purchase path uses,
  requires a positive type-specific quantity, invents no default, requires a positive price,
  guards duplicate codes, and there is deliberately **no delete route**. Left alone apart from A6.
- **Plan change governance (Wave 2).** Upgrade = immediate after confirmed payment with chained
  validity; downgrade = `pending_plan_id` + `pending_plan_effective_at` applied at the term
  boundary by `apply_pending_plan_change_if_due()` via `check_user_limits()`;
  `materialize_plan_entitlements()` preserves purchased and admin-granted ledger entitlement;
  nothing is deleted. Correct as built — **not rewritten**, only re-asserted by a test.
- **Purchase-time policy snapshot.** `PaymentOrder.plan_policy_snapshot_json` already isolates
  historical contracts from live plan edits. No change needed; a test now proves it.
- **Central resolver.** `get_effective_entitlements(...)` remains the only entitlement system.
  No second resolver, no second grant ledger.
- **Storage grant/revoke (Wave 3), scan grant (Wave 1), coverage admin-grant (Wave 4).**
  Already ledgered, audited and source-distinguishable. Generalised, not replaced.
- **Refund workflow.** Full-refund-only, admin-only, provider-confirmed-before-reversal,
  idempotency key, non-destructive reconciliation with explicit
  `MANUAL_REVIEW_REQUIRED` branches. Scope unchanged.
- **Permission architecture.** Every Wave 5 action mapped onto an existing stable code. **No new
  permission code was added** — `admin.commercial.*` would have been redundant.
- **CSRF / POST-only.** `CSRFProtect` is global with `WTF_CSRF_CHECK_DEFAULT = True`; no
  `@csrf.exempt` on any admin commercial route.
- **Ownership resolution.** Audited specifically for the Wave 4 regression class and found
  **correct**: `set_project_current_owner()` mirrors `owner_user_id` to the new owner and
  backfills `created_by_user_id`, so admin views reading `owner_user_id` show the *current*
  owner, `manager_vendor_user_id` is never conflated with owner, `beneficiary_user_id` is
  never conflated with owner, and transfer grants no coverage. **No real bug found here.**
- **AdminActivity coverage** of plan/add-on/grant/ownership/coverage actions.

### C — certification / hardening (not implemented)

- A PostgreSQL `CHECK` constraint mirroring the Python `@validates` on `plan_family` /
  `lifecycle_status` (application-level validation is authoritative today).
- Full PostgreSQL certification lane and one serial full regression.
- Load/perf characterisation of `/admin/api/refunds` at production row counts.

### D — UX-only / Agent 2 (not implemented)

- Richer admin presentation of the entitlement summary, storage meters and ownership panels.
- Customer-facing plan-comparison and lifecycle messaging.
- An admin refund *page* on top of the `/admin/api/refunds` endpoint.

### E — future / out of scope (not implemented)

- Partial refunds, invoicing, plan versioning as a separate revision table, self-service account
  conversion, organisation/workspace/seat modelling, add-ons for per-file limits, pair limits or
  experience entitlements (locked plan differentiators since Wave 2).

## 6. Plan-admin work completed

`admin_add_plan` and `admin_edit_plan` were rewritten around **one shared parser**,
`_plan_form_values(form, existing=None)`, so a plan cannot be created through one door that the
other would reject:

- Governs the full commercial definition: name, description, currency, amount, offer price,
  duration type/value, trial days, project/scan limits (with the long-standing "unlimited"
  checkbox → `NULL` semantics preserved), pairs per project, display order, features,
  **`plan_family`, `lifecycle_status`, `base_storage_bytes`, `max_image_bytes`,
  `max_video_bytes`, `max_video_duration_seconds`, `max_image_dimension_px`,
  `max_image_pixels`, `allow_direct_qr`, `allow_detect_once`, `allow_tracked_overlay`.**
- Rejects: negative amounts and limits, `max_pairs_per_project < 1`, `duration_value < 1`,
  non-numeric values (reported, no longer silently swallowed), `plan_family` outside
  `PLAN_FAMILIES`, `lifecycle_status` outside `PLAN_LIFECYCLE_STATUSES`, `duration_type` outside
  `{time, count}`, `offer_price > plan_amount`, and a plan that allows **no** experience.
- **Absent ≠ blank.** A field not present in the submitted form is left untouched
  (`_PLAN_UNSET`), so a partial or older form can never blank a column it does not render.
  Checkbox groups carry hidden markers (`plan_flags_form`, `plan_experience_form`) because an
  unchecked box is simply absent from a POST.
- `_apply_plan_values()` reports whether any field in `PLAN_REVISION_TRACKED_FIELDS` moved;
  `admin_edit_plan` bumps `plan_revision` only then. Presentation-only edits (name, description,
  features, ordering, popularity) do not bump it. The audit line records every field transition.
- `SubscriptionPlan` was **not redesigned** — Wave 2's lifecycle/revision foundation is used
  as-is. No second versioning architecture.

`admin_delete_plan` now consults `plan_commercial_references(plan)` — subscribers,
pending plan changes and payment orders — and refuses a hard delete if any exist, directing the
operator to archive instead. Unreferenced plans still delete.

`POST /admin/plans/<id>/lifecycle` is the new governed lifecycle control: validates against
`PLAN_LIFECYCLE_STATUSES`, bumps the revision, audits `previous -> new`, and is non-destructive
by construction — it only gates `is_purchasable`.

## 7. Plan-lifecycle work completed

Wave 2's upgrade/downgrade mechanism was audited and found **correct (classification B)** and
was **not rewritten**. The one genuine gap was that the lifecycle vocabulary was never enforced:

- `purchasable_plans_query()` added — the query form of `is_purchasable`. All three
  customer-facing plan listings (`/`, `/pricing`, `/subscribe`) now use it.
- `/create-razorpay-order` now validates `plan.is_purchasable` instead of `plan.is_active`, so
  posting the id of a `DRAFT` / `CLOSED_FOR_NEW_PURCHASE` / `ARCHIVED` plan is rejected before
  any Razorpay order or capacity reservation exists.
- Existing subscribers are untouched by any lifecycle move: term, projects, media, QR codes and
  entitlement resolution all continue from the same plan row. Tested.

## 8. Add-on governance completed

`/admin/addons` was already governed (classification B) and was left intact. One real gap closed:

**`addon_type` is now immutable once the SKU has been purchased.** `_addon_catalog_form_values`
rejects a type change when any `AddonPurchase` references the row, because
`_apply_refund_reconciliation()` re-reads `item.addon_type` to choose the reversal branch — a
type edit would have reversed the wrong entitlement on every historical purchase. Every other
field of a sold item stays editable; the guidance is to deactivate and create a new add-on.

Supported types confirmed unchanged: `EXTRA_SCANS`, `PROJECT_CAPACITY`, `ACCOUNT_STORAGE`,
`PROJECT_SERVICE_COVERAGE`, plus the pre-existing internal `VALIDITY_EXTENSION` (still in
`ADDON_PURCHASABLE_TYPES` and still routed to `MANUAL_REVIEW_REQUIRED` on refund — left exactly
as found). No per-file-limit, pair-limit or experience-entitlement add-on was added.

## 9. Admin entitlement / grant work completed

`grant_account_storage()` was generalised into **`grant_account_entitlement(admin, user,
entitlement_type, delta, reason)`**, covering `ACCOUNT_STORAGE`, `PROJECT_CAPACITY` and
`EXTRA_SCANS` through the **existing** `EntitlementTransaction` ledger and
`_ent.ADMIN_GRANT_SOURCE_TYPE`. No second grant ledger. `grant_account_storage()` is kept as a
thin wrapper so Wave 3 callers are unchanged.

- New `POST /admin/users/<id>/grant-project-capacity` — signed delta, grant and revoke.
- `POST /admin/scans/<id>/grant-extra` now accepts a **negative** amount as a governed revoke
  (it previously hard-rejected anything `<= 0`) and routes through the shared helper.
- Zero is rejected everywhere rather than writing an empty ledger row.
- Each adjustment writes an `AdminActivity` row first and uses its id as the ledger
  `source_id`, so grants stay auditable and distinguishable from purchased entitlement.
- **Revocation is never destructive**: it lowers the allowance only. No project, media object or
  QR code is touched. Over-capacity is allowed and simply blocks *new* consumption.
- `_apply_entitlement_transaction` now clamps `subscribed_scan_limit` and
  `subscribed_project_limit` at **zero** on negative deltas — a negative column would have been
  read by `_limit_reached()` as unlimited. This also hardens the pre-existing refund-reversal path.

## 10. Account commercial-control work completed

Admin inspection was already comprehensive via `user_entitlement_summary()` (account type, plan,
plan family + label, lifecycle status, revision, base/purchased/effective project and scan
capacity, base/purchased/admin-granted/effective/used/remaining storage, over-capacity and
over-storage flags, per-file media policy, per-experience entitlements, pending downgrade and its
effective date) rendered on both `view_user.html` and `user_dashboard_context.html`. **Classified
B — not rebuilt.**

Wave 5 added the missing *controls* on that page: project-capacity grant/revoke and account-type
conversion, alongside the existing storage grant/revoke. Project-level coverage and ownership
context is now inspectable on the admin project page (§13).

## 11. Account conversion work

Implemented as a **small Admin-only governed HTTP route**, exactly as the brief's safe default:

`POST /admin/users/<id>/account-type`, gated by the existing `admin.ownership.manage`
permission (the Wave 4 dependencies it consults are ownership-domain, so no new permission code
was needed).

- Validates the target against `USER_ACCOUNT_TYPES`.
- **INDIVIDUAL → BUSINESS_VENDOR**: flips `account_type` and nothing else. Projects, media, QR
  codes, purchases, the entitlement ledger, the storage ledger, ownership history and the
  subscription are all preserved. `plan_family` remains a separate axis, so the plan is **not**
  reassigned.
- **BUSINESS_VENDOR → INDIVIDUAL**: calls Wave 4's `can_convert_to_individual(user)` and refuses
  when vendor-managed projects, active transfers or open claims exist. A blocked conversion
  **severs nothing** — the account simply stays a vendor.
- Audited via `AdminActivity` with actor, target, `previous -> target` and an optional reason.
- **No self-service conversion route was built.**

## 12. Ownership / commercial consistency fixes

I audited this specifically and found **no real ownership-resolution bug** —
`set_project_current_owner()` mirrors `owner_user_id` onto the new owner while backfilling
`created_by_user_id`, so every admin view that reads `owner_user_id` was already showing the
*current* owner, `manager_vendor_user_id` is never treated as owner, `beneficiary_user_id` is
never treated as owner, and transfer does not create, move or grant coverage. Nothing was
"fixed" that was not broken.

The genuine gap was *legibility*: the admin project page showed a single unlabelled "Owner" with
no way to tell a transferred project from an original one. `admin_view_project` now also passes
`creator` (from `project_created_by_user_id`) and `project_ownership_context(...)`, and the
template renders **Owner (current, carries account entitlement responsibility) / Created By
(permanent history) / Managing Vendor / Beneficiary** as four distinct rows. Covered by a test
that performs a real ownership move and asserts both parties appear.

## 13. Coverage-admin work

`ProjectServiceCoverage` had no admin inspection surface. `admin_view_project` now renders a
Service Coverage panel:

- Resolved state from the existing `project_coverage_summary()`: publicly live, reason, coverage
  source, effective coverage until, renewal anchor, renewal eligibility / blocking code.
- The last 25 coverage rows with **source type + reference, start, end (or "Indefinite"),
  status, grantor (admin / user / system) and reason.**

Read-only. No new coverage engine — Wave 2's resolver and Wave 4's
`admin_grant_project_service_coverage()` remain the only mechanisms. The existing admin grant
path already validates a finite positive duration and a mandatory reason, writes an auditable
`ADMIN_GRANT` row anchored to `project_renewal_anchor()`, changes no QR code, deletes no
project or media, does not double-charge subscription-backed coverage and leaves Wave 2's
`LEGACY_COMPATIBILITY` blocking rule intact. Tested, not modified.

## 14. Refund-admin work

Scope **unchanged** — still admin-only, still full-refunds-only, no partial refund path exists
or was added. One operational-visibility gap closed:

`GET /admin/api/refunds` (read-only, `admin.payments.view`) — paginated, filterable by
`status`, `reconciliation_status`, `user_id`, and `needs_attention=1` which surfaces exactly
the refunds an operator must chase: `REFUND_FAILED`, or reconciliation in `PENDING` / `FAILED` /
`MANUAL_REVIEW_REQUIRED`. Unknown filter values return `400` rather than silently matching
nothing. It reuses the existing `_payment_refund_payload()` (provider refund id, provider status,
reason, admin actor, requested/completed/failed timestamps, failure code, safe messages), so no
new field and no new refund UI was invented.

The zero-clamp in §9 also hardens the refund reversal path, which subtracts from the same
materialised columns.

## 15. Permission changes

**None.** Every Wave 5 action mapped onto an existing stable code:

| Action | Permission |
|---|---|
| plan add / edit / delete / toggle / lifecycle | `superadmin.plans.manage` |
| add-on create / edit / toggle | `superadmin.addons.manage` |
| entitlement grant/revoke (scans, capacity, storage) | `admin.users.manage` |
| account-type conversion | `admin.ownership.manage` |
| refund list / detail / eligibility | `admin.payments.view` |
| refund execution | `admin.payments.refund` |
| coverage grant | `superadmin.capacity.manage` |

`admin.commercial.view` / `.manage` were **deliberately not added** — they would have been
redundant with `superadmin.plans.manage` / `superadmin.addons.manage`. All gating goes through
`require_admin_permission(...)`; no hard-coded role-name check was introduced.

## 16. Audit / history changes

All via the existing `AdminActivity` / `log_admin_activity(...)`. No secrets are logged.

| Activity type | Recorded |
|---|---|
| `plan_add` | plan id, name, family, lifecycle, revision |
| `plan_edit` | plan id, name, new revision, **every changed commercial field as `old -> new`** (or "presentation only") |
| `plan_delete` | only ever fires for a genuinely unreferenced plan |
| `plan_toggle` | plan id + name |
| `plan_lifecycle_change` *(new)* | plan id, name, `previous -> new`, new revision |
| `addon_create` / `addon_edit` / `addon_toggle` | unchanged |
| `project_capacity_grant` *(new)* | signed amount, user email, reason |
| `extra_scans_grant` | now records grant **and** revoke with reason |
| `account_storage_grant` | unchanged shape |
| `account_type_change` *(new)* | user id + email, `previous -> target`, reason |
| `project_coverage_grant` | unchanged |
| ownership transfer/claim reviews | unchanged |

## 17. Schema / migration changes

**None.** No new column, table, index or constraint. Alembic head remains **`a9d3c7e1b502`**,
unchanged from Wave 4 and re-verified after the develop sync. Every field Wave 5 governs already
existed on `SubscriptionPlan` from Wave 2 — the gap was that no admin form wrote them.

## 18. HTTP routes added / changed

**Added (3):**

| Route | Method | Permission |
|---|---|---|
| `/admin/plans/<int:plan_id>/lifecycle` | POST | `superadmin.plans.manage` |
| `/admin/users/<int:user_id>/grant-project-capacity` | POST | `admin.users.manage` |
| `/admin/users/<int:user_id>/account-type` | POST | `admin.ownership.manage` |
| `/admin/api/refunds` | GET (read-only) | `admin.payments.view` |

**Changed (7):** `/admin/plans/add`, `/admin/plans/<id>/edit`, `/admin/plans/<id>/delete`,
`/admin/addons/<id>/edit`, `/admin/scans/<id>/grant-extra`, `/create-razorpay-order`,
`/admin/projects/<id>` (added coverage + ownership context). Plan listings on `/`, `/pricing`
and `/subscribe` now filter on purchasability.

No GET mutation was introduced. `add` and `edit` keep GET only to render their forms.

## 19. Validation / security changes

- Every plan field validated **server-side**, never relying on HTML `min` / `required`.
- Rejected: negative commercial limits, `max_pairs_per_project < 1`, `duration_value < 1`,
  invalid `plan_family`, invalid `lifecycle_status`, invalid `duration_type`, `offer_price >
  plan_amount`, a plan allowing no experience, non-numeric input (now reported instead of
  silently ignored), invalid add-on type, missing type-specific add-on quantity, add-on type
  change after purchase, invalid account type, unsafe vendor→individual conversion, zero-delta
  entitlement adjustment, unknown refund filter values.
- Malformed ids are handled by Flask's `<int:...>` converters plus `get_or_404`.
- Bare `except Exception` + `traceback.print_exc()` in the plan routes replaced with narrow
  `(ValueError, SQLAlchemyError)` handling, a `db.session.rollback()`, a safe operator-facing
  message and a server-side log line — the old code leaked raw exception text into a flash.
- All state-changing admin commercial actions are POST-only and CSRF-protected by the global
  `CSRFProtect`. No process-local authorisation or rate-limit hack was introduced; the Wave 1
  centralised mechanisms are reused.
- Negative-delta clamping prevents a revocation from producing a negative limit column that
  enforcement would read as unlimited.

## 20. Focused test results

New: `tests/integration/test_wave5_admin_commercial_completion.py` — **38 tests, all passing**
(709 lines). Coverage maps 1:1 onto the brief's §15 list:

- **Plan admin**: valid full-policy creation; 12 parametrised invalid/negative configurations
  rejected; safe edit; revision bumps only on commercial change; delete refused for a plan
  referenced only by payment history; delete allowed for an unreferenced plan; **historical
  payment snapshot proven unchanged after a live plan edit**.
- **Plan lifecycle**: invalid status rejected; valid change bumps revision and flips
  `is_purchasable`; subscriber, project and QR preserved; non-purchasable plan hidden from
  listings and rejected at checkout with no `PaymentOrder` created; Wave 2 downgrade boundary
  semantics re-asserted (not due → no change; due → applied, limits lowered, pending cleared).
- **Add-ons**: create supported type; invalid type rejected; missing type-specific delta
  rejected (nothing defaulted); type immutable after purchase but mutable before; other fields of
  a sold item still editable; no destructive delete route exists.
- **Grants**: project-capacity grant and revoke ledgered with `admin_grant` source; zero rejected;
  purchased vs admin-granted stay separate and a revoke does not erase the purchased row;
  storage grant/revoke through the shared helper; over-revocation clamps at zero.
- **Account type**: Individual→Vendor preserves subscription, entitlements, project and QR and
  writes an audit row; Vendor→Individual blocked by a Wave 4 dependency **without severing the
  relationship**, then succeeds once the dependency is cleared; invalid value rejected.
- **Coverage**: admin project page exposes source, window, status, grantor and reason; grant
  rejects zero days and empty reason; successful grant is audited; project untouched.
- **Ownership consistency**: after a real ownership move the admin page shows the current owner
  **and** the preserved creator.
- **Refunds**: list surfaces `MANUAL_REVIEW_REQUIRED`; `needs_attention` and
  `reconciliation_status` filters correct; invalid filter → 400; endpoint is GET-only.
- **Security**: no GET mutation among the Wave 5 routes; CSRF enforced (returns 400 and leaves
  state unchanged); non-superadmin blocked from plan and add-on governance; a logged-in **user**
  cannot invoke any admin commercial action.

Narrowly related existing lanes re-run — **all green**:

| Lane | Result |
|---|---|
| `test_wave5_admin_commercial_completion.py` | **38 passed** |
| `test_admin_refunds.py` + `test_addon_entitlements.py` + `test_wave2_entitlement_foundation.py` + `test_admin_crud_hardening.py` + `test_super_admin_authorization.py` | **91 passed** |
| `test_domain_ownership_foundation.py` + `test_domain_commercial_capacity_and_reporting.py` + `test_v1_agent2_admin_parity.py` + `test_payment_and_admin_baseline.py` | **91 passed** (1 pre-existing assertion updated, see below) |
| `test_wave1_p0_blockers.py` + `test_wave3_storage_accounting.py` | **117 passed** |

**Total: 337 focused/related tests passing.**

One existing assertion was deliberately inverted:
`test_v1_agent2_admin_parity.py::test_admin_plan_pages_expose_policy_contract_without_unbacked_inputs`
asserted that `name="plan_family"` and `name="lifecycle_status"` must **not** appear as form
inputs — a correct Wave 2-era statement ("still read-only in this UI"). Wave 5's brief requires
exactly the opposite. The test now asserts those inputs **are** present and backed, keeps the
forbidden list for the genuinely unbacked names (`base_storage`, `media_policy`,
`experience_entitlements`, `revision_status`), and the corresponding `plans.html` copy was
updated so the page no longer claims the fields are read-only.

## 21. Tests deliberately NOT run

- The full suite (`python -m pytest -q`) — forbidden by the brief.
- The full PostgreSQL certification lane.
- Scanner/vision lanes (`gate_e`–`gate_jr`, compatibility, performance) — Wave 5 touched no
  scanner file, so they are the project lead's single serial regression.
- Upload, resumable-upload, processing/RQ, webhook, security and migration lanes not touched by
  Wave 5.

## 22. Known limitations / deferred certification items

1. **No database-level CHECK constraint** on `plan_family` / `lifecycle_status`. Python
   `@validates` is authoritative; direct SQL writes could still bypass it. Classified **C**.
2. **`plan_revision` is a counter, not a history table.** There is no per-revision archive of
   past plan definitions; the immutable record of what a customer bought remains
   `PaymentOrder.plan_policy_snapshot_json`. Deliberate — a second versioning architecture was
   explicitly out of scope. Classified **E**.
3. **`/admin/api/refunds` is an API, not a page.** Operators use it via URL or tooling; a
   rendered refund queue is Agent 2 / **D**.
4. **Failed refund reconciliation has no retry action.** The endpoint makes it *visible*;
   re-driving it remains a manual operator task, as before. Expanding refund behaviour was out
   of scope.
5. **Deferred downgrade still applies on the next gated request**, not via a scheduled sweep —
   Wave 2's documented ceiling, unchanged and harmless (an unapplied downgrade only ever leaves
   the account on a *higher* allowance).
6. **`plan_family` is not cross-checked against `account_type` at checkout.** The locked
   architecture states plan families remain distinct from account type, so no such gate was
   invented. If a coupling is ever wanted, it is a policy decision, not a bug fix.
7. **`VALIDITY_EXTENSION`** remains in `ADDON_PURCHASABLE_TYPES` and still routes to
   `MANUAL_REVIEW_REQUIRED` on refund. Left exactly as found; its commercial status is a product
   decision for certification.
8. **Storage/scan/capacity admin forms take raw integers** (e.g. bytes). No unit picker —
   **D**.
9. Wave 5 was validated on SQLite only. PostgreSQL certification is the project lead's.

## 23. Merge risk

**LOW-MODERATE.**

Low because: no migration, no schema change, no new dependency, no new permission code, no
scanner file touched, the central resolver untouched, every locked commercial policy preserved,
and 337 focused/related tests pass.

Moderate only because: `app.py` plan-admin routes were rewritten rather than patched (a ~320-line
region replaced), and five admin templates changed — one of which (`view_user.html`,
`plans.html`) is in a family Agent 2 is also editing in parallel, so a textual merge conflict in
those templates is plausible. The conflict surface is additive blocks, not restructured markup.
One existing Wave 2-era test assertion was intentionally inverted and is documented in §20.

## 24. git status

```
$ git -c safe.directory="F:/ScanStory-main/ScanStory-v1.1-agent1" status --short
(empty — clean)
```

All Wave 5 code, tests and this report are committed. Nothing pushed, nothing merged.

## 25. Scanner files untouched — confirmation

```
$ git diff --stat 49bb5ee6c5a2a4356e4bbd304f31d03bf0584c5c -- \
    scanner_runtime.py media_processing.py compatibility_resolver.py \
    static/js/scanner-runtime.js templates/user/scanner.html
(empty)
```

**Byte-identical to the starting commit.** No ORB / homography / RANSAC / optical-flow /
camera-logic / reacquisition / fallback / threshold / smoothing value was read, moved or changed.

## 26. Published V1 refs untouched — confirmation

No branch, tag or ref other than `agent/v1.1-platform-admin` was written. `release/scanstory-v1-server`,
`hardening/saas-v1-production`, `v1.0.0-rc1` and `v1.0.0-rc2` were never checked out, reset,
tagged or pushed. `develop/scanstory-v1.1` was only **read** (fast-forwarded *into* my branch);
it was not moved. `F:\ScanStory-main\ScanStory-integration` was never touched. No `git push`,
no `git merge` into any shared branch. `safe.directory` was passed per-command; **no global git
config was modified.** No secret was printed, logged, staged or committed.

## 27. Final assessment — is feature development complete after Wave 5?

**Yes.** Wave 5 closed the last structural gaps between the commercial *model* and the commercial
*operations* around it. Every entitlement Wave 2–4 defined can now be created, edited,
lifecycle-governed, granted, revoked, inspected and audited by an Admin through a validated,
permission-gated, CSRF-protected HTTP surface — with no hand-editing of the database left as a
required operational step.

The four gaps that mattered most were not missing features but **unenforced ones**:
`is_purchasable` was dead code, `plan_revision` was never bumped, the Wave 2 plan-policy columns
had no writer, and `can_convert_to_individual()` had no caller. Wave 5 wired all four up rather
than building anything new, and left the eight already-correct subsystems (add-on validation,
plan-change governance, the central resolver, purchase-time snapshots, storage/scan/coverage
grants, the refund workflow, the permission architecture, ownership resolution) untouched.

Every locked policy holds: two account types, plan families distinct from account type, one
resolver, one grant ledger, reusable account-level capacity separate from project coverage,
validity rules unchanged, expiry non-destructive, QR preserved across renewal, legacy indefinite
projects still compatibility-governed, and refunds still admin-only and full-only with
non-destructive reconciliation.

What remains is **certification and hardening, not feature work**: the PostgreSQL lane, one
serial full regression, the optional database-level CHECK constraints, and Agent 2's UX
reconciliation. I recommend the project lead proceed to the combined Wave 4/5 integration gate.

**Verdict: WAVE 5 COMPLETE — FEATURE DEVELOPMENT READY FOR CERTIFICATION.**
