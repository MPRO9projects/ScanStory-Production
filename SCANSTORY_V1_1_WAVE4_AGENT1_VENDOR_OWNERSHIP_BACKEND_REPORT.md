# ScanStory V1.1 — Wave 4 (Agent 1): Vendor / Business Ownership Backend

Branch: `agent/v1.1-platform-admin`
Worktree: `F:\ScanStory-main\ScanStory-v1.1-agent1`

---

## 1. Starting commit

`0a5011c808bf410603f927ec70ab166c4498ed82` (post-sync).

Pre-work verification, all run with `-c safe.directory="F:/ScanStory-main/ScanStory-v1.1-agent1"`:

| Check | Result |
|---|---|
| `git status --short` | clean |
| `git branch --show-current` | `agent/v1.1-platform-admin` |
| `git rev-parse HEAD` (before sync) | `44185c6a3fedd6dfab4e66ac5b4615b466cb801a` |
| `git rev-parse develop/scanstory-v1.1` | `0a5011c808bf410603f927ec70ab166c4498ed82` |
| `git merge develop/scanstory-v1.1` | **fast-forward**, no conflicts |

Wave 3 symbols confirmed present in the synced tree before any Wave 4 code was written:
`MediaObject` and `ACCOUNT_STORAGE` (models.py, storage_accounting.py, entitlements.py),
`evaluate_project_storage_transfer` (app.py:3691 pre-edit), `move_project_storage_ownership`
(storage_accounting.py:228), migration head `f2b7d4e9c3a6`.

## 2. Ending commit

`ee531f59587d683bc47692023635b48cd8b6f779`

## 3. Files changed

| File | Change |
|---|---|
| `app.py` | Wave 4 service layer, user HTTP surface, Admin review surface, permission codes, rate-limit scope, quota-release primitive |
| `models.py` | `ProjectOwnershipClaim.metadata_json` (one nullable Text column) |
| `migrations/versions/a9d3c7e1b502_ownership_claim_audit_metadata.py` | **new** — additive column migration |
| `templates/user/ownership.html` | **new** — the functional ownership surface (accept / decline / withdraw / initiate / claim) |
| `templates/admin/ownership.html` | **new** — Admin review queue |
| `templates/admin/_sidebar_links.html` | one nav entry, gated on `admin.ownership.view` |
| `tests/integration/test_wave4_vendor_ownership_backend.py` | **new** — 27 focused tests |

Deliberately **not** touched: `templates/user/project_preview.html` (Agent 2's state-display-only
ownership panel and its "not available from here yet" copy). Those routes now exist, so that copy is
obsolete — updating it is the next Agent 2 checkpoint, per this wave's brief. Nothing in this wave
contradicts it; the new controls live on a separate page.

## 4. Existing ownership schema reused

Everything. No second ownership system was introduced.

- `Project`: `created_by_user_id`, `current_owner_user_id`, `manager_vendor_user_id`,
  `beneficiary_user_id`, legacy `owner_user_id` — all five kept distinct.
- `ProjectOwnershipTransfer`: `project_id`, `initiated_by_user_id`, `from_owner_user_id`,
  `to_user_id`, `retain_vendor_management`, `status`, `created_at`, `accepted_at`, `completed_at`,
  `cancelled_at`, `expires_at`, `completed_by_admin_id`, `reason`, `note`, `metadata_json`.
- `ProjectOwnershipClaim`: `project_id`, `claimant_user_id`, `current_owner_user_id`, `status`,
  `evidence_summary`, `evidence_json`, `vendor_notified_at`, `response_deadline_at`, `reviewed_at`,
  `reviewed_by_admin_id`, `decision_reason`, `transfer_id`.
- Helpers: `project_current_owner_user_id`, `project_created_by_user_id`, `user_can_manage_project`,
  `user_can_transfer_project`, `project_user_access_filter`, `_active_project_transfer`,
  `set_project_current_owner`, `is_business_vendor`.
- Capacity (Wave 1): `effective_project_limit`, `_limit_reached`, `_reserve_project_quota_atomic`,
  `_atomic_increment_user_counter`, `has_dev_test_entitlement`.
- Storage (Wave 3): `evaluate_project_storage_transfer`, `account_storage_state`,
  `storage_accounting.move_project_storage_ownership`, `storage_accounting.project_counted_bytes`.
- Status vocabularies: `PROJECT_TRANSFER_STATUSES`, `PROJECT_ACTIVE_TRANSFER_STATUSES`,
  `PROJECT_CLAIM_STATUSES`, `PROJECT_ACTIVE_CLAIM_STATUSES` — **no new status codes added**.
- Admin: `ADMIN_ROLE_PERMISSIONS`, `admin_has_permission`, `require_admin_permission`, `admin_can`,
  `log_admin_activity` / `AdminActivity`.
- Rate limiting: `RATE_LIMITS` + `_check_rate_limit` / `_rate_limit_key` (Wave 1 P0-8 centralized limiter).
- CSRF: app-wide `CSRFProtect(app)` — no new route is `@csrf.exempt`.

## 5. Schema / migration changes

**One column. Revision `a9d3c7e1b502`, down_revision `f2b7d4e9c3a6` (linear from the current head).**

`project_ownership_claims.metadata_json TEXT NULL`.

Why it was genuinely necessary: `project_ownership_transfers` already had `metadata_json`, which
Wave 4 uses for the governed-transition trail and for the PENDING_CAPACITY failure snapshot. Claims
had no equivalent, and the alternatives were worse — `decision_reason` is the Admin's field and
`evidence_json` is the claimant's; overloading either with the vendor response note and the claim's
transition history would collapse three separate concepts into one column.

PostgreSQL compatible: nullable TEXT, no default, no backfill, `batch_alter_table` for SQLite's
table-rebuild semantics, idempotent guard (`_has_column()`) on both `upgrade()` and `downgrade()`.
Additive only — nothing renamed, retyped, dropped or rewritten. No historical migration was edited.

**No migration was needed for PENDING_CAPACITY**: the failing-dimension snapshot fits in the transfer
row's existing `metadata_json`, and the state itself already existed.

## 6. Transfer lifecycle

States used — **entirely the existing vocabulary**:

| State | Meaning in Wave 4 |
|---|---|
| `PENDING_ACCEPTANCE` | Initiated, waiting for the recipient |
| `PENDING_CAPACITY` | Recipient cannot absorb it yet (project slots and/or storage). Recoverable |
| `COMPLETED` | Ownership + storage responsibility moved, in one transaction |
| `CANCELLED` | Recipient declined **or** sender withdrew **or** Admin cancelled (the trail records which) |
| `EXPIRED` | `expires_at` had passed when acceptance was attempted |
| `DISPUTED` | Frozen by an Admin for manual review; current owner stays authoritative |

There is no `REJECTED` transfer state in the repo's vocabulary, so a recipient decline resolves to
`CANCELLED` with a `transfer_rejected` audit event rather than inventing a code the model's
`@validates` would reject.

Service functions (`app.py`):

- `initiate_project_ownership_transfer(...)` — existing, extended with an audit event.
- `accept_project_ownership_transfer(transfer, acting_user=None, completed_by_admin=None)` — rewritten.
- `reject_project_ownership_transfer(transfer, acting_user, reason=None)` — new.
- `cancel_project_ownership_transfer(transfer, acting_user=None, admin=None, reason=None)` — new.
- `mark_project_transfer_disputed(transfer, admin, reason=None)` — new.
- `release_project_transfer_dispute(transfer, admin, reason=None)` — new.
- `evaluate_transfer_capacity(project, recipient)` — new, read-only, reserves nothing.
- `transfer_capacity_snapshot(transfer)` / `ownership_audit_trail(record)` — new readers.

Transfer remains **explicit at every step**. A beneficiary, a matching email, a claim, or the vendor
having created the project never moves ownership on its own — verified by
`test_transfer_is_never_automatic_from_beneficiary_or_creator`.

## 7. Project-capacity handling

Reuses Wave 1 unchanged. `evaluate_transfer_capacity()` reads `effective_project_limit(recipient)`
(base plan + purchased reusable `PROJECT_CAPACITY` ledger deltas + governed grants, materialized on
`User.subscribed_project_limit`) and compares with `_limit_reached()`; the actual consumption goes
through `_reserve_project_quota_atomic(recipient)`, the same single conditional UPDATE Wave 1 built.

- Recipient's slot is consumed **only** at real completion, never at initiation.
- Sender's slot is freed **only** at real completion, via the new
  `_release_project_quota_atomic(user)` — the exact mirror of the reservation (clamped at zero with
  `case()`, skipped for dev-test entitlement accounts, same as the reservation's own skip). This
  replaces the previous non-atomic `sender.projects_used = max(0, ... - 1)` read-modify-write.
- Purchased capacity stays reusable, not lifetime-consumed. No existing purchased-capacity logic was
  rewritten.

## 8. Storage-capacity handling

Reuses Wave 3 **unmodified**. `storage_accounting.py` was read in full and **not edited**; no bug was
found in it, and no byte arithmetic was hand-rolled in this wave.

`accept_project_ownership_transfer()` calls `evaluate_project_storage_transfer(project, recipient)`
(which delegates to `storage_accounting.evaluate_storage_transfer` → `project_counted_bytes` +
`can_consume`) **before** reserving the project slot. That ordering is deliberate: an
insufficient-storage recipient means no counter was ever incremented, so nothing has to be unwound.

## 9. MediaObject ownership movement

On completion only, inside the same transaction as the ownership change:
`_storage.move_project_storage_ownership(project.id, sender.id, recipient.id)`.

Verified by reading Wave 3's implementation before relying on it: it reassigns
`MediaObject.owner_user_id` for the project's ACTIVE rows, releases the counted bytes from the
sender's `storage_used_bytes` and adds them to the recipient's. **No physical file is copied, moved
or deleted** — billing responsibility changes, bytes stay where they are on disk. Confirmed by
`test_completed_transfer_moves_media_ownership_exactly_once_and_deletes_nothing`, which asserts the
image and video files still exist on the filesystem afterwards.

## 10. PENDING_CAPACITY behaviour

Non-destructive by construction. When either dimension fails,
`_park_transfer_pending_capacity(transfer, snapshot)`:

- transitions the row to `PENDING_CAPACITY` (conditional UPDATE),
- writes a `capacity_block` snapshot into `metadata_json` recording **which dimension(s) failed and
  at what checked values** — `storage_ok`, `project_slot_ok`, `project_bytes`,
  `recipient_storage_used_bytes`, `recipient_storage_allowance_bytes`, `recipient_project_limit`,
  `recipient_projects_used`, `checked_at` — so a retry does not re-derive it from scratch,
- appends a `transfer_pending_capacity` audit event.

Guarantees: no partial ownership move, no partial MediaObject movement, no deletion, and the sender
keeps their project slot and storage responsibility. Recovery is the **same transfer row**: calling
`accept_project_ownership_transfer()` again once capacity exists completes it, with no duplicate
ownership transition and no second transfer row
(`test_pending_capacity_transfer_completes_once_capacity_appears` asserts
`ProjectOwnershipTransfer.query.count() == 1`).

The existing state was extended in meaning rather than replaced: it already meant "recipient cannot
absorb this yet", and storage is a second capacity *dimension*, not a second state. A new status
would also have broken Agent 2's existing state-display UI and the model's `@validates` set.

## 11. Claim lifecycle

States used — again entirely existing: `OPEN` → (`VENDOR_NOTIFIED`) → `APPROVED_BY_VENDOR` |
`PENDING_ADMIN_REVIEW` → `APPROVED_BY_ADMIN` | `REJECTED`, plus `CANCELLED`, `EXPIRED`, and
`TRANSFER_COMPLETED` as the terminal state once the linked transfer actually completes.

- `create_project_ownership_claim(...)` — existing; now records a `claim_submitted` audit event.
  Duplicate open claims still de-duplicate to the same row.
- `respond_to_project_ownership_claim(claim, acting_user, accept, response_note=None)` — new.
- `cancel_project_ownership_claim(claim, acting_user, reason=None)` — new (claimant withdraws).
- `approve_project_ownership_claim_by_admin(...)` — existing, rewritten onto the conditional-UPDATE
  gate and now resolving the **live** current owner rather than the snapshot taken at claim time.
- `reject_project_ownership_claim_by_admin(claim, admin, decision_reason=None)` — new.
- `_mark_claims_transfer_completed(transfer)` — closes linked claims exactly once on completion.

A claim never moves ownership. Approval only **opens a transfer**, which the recipient must accept
and which still passes both capacity gates —
`test_admin_approval_does_not_transfer_and_still_respects_capacity` asserts that an Admin-approved
claim against a full recipient lands in `PENDING_CAPACITY` with the original owner intact.

## 12. Vendor response

`respond_to_project_ownership_claim()` is scope-enforced in the backend via
`user_can_respond_to_claim(user, claim)` → `user_can_manage_project(user, project)` (current owner or
explicit `manager_vendor_user_id` who is a `BUSINESS_VENDOR`), and explicitly refuses the claimant
responding to their own claim. An unrelated vendor raises `PermissionError`, and the HTTP route
returns 404 rather than confirming the claim id exists.

- **Accept** = consent, not a transfer: status `APPROVED_BY_VENDOR` and a normal
  `PENDING_ACCEPTANCE` transfer is opened for the claimant to accept.
- **Refuse** = escalate, not close: status `PENDING_ADMIN_REVIEW`. A counterparty is never the
  adjudicator, so a refusal cannot terminate someone else's claim.

A vendor cannot force-transfer an unrelated project, cannot bypass the recipient's capacity checks
(the same `accept_project_ownership_transfer()` gate applies), and cannot Admin-resolve anything —
the Admin routes are behind `require_admin_permission` and a vendor has no admin session.

## 13. Admin review / dispute flow

Page: `GET /admin/ownership` renders, per transfer and per claim, the creator / current owner /
managing vendor / beneficiary, the from→to parties, the status, the capacity-failure snapshot, and
the full audit trail. No secrets and no filesystem paths are exposed (parties are shown by account
email, media is not listed).

Actions: approve claim, reject claim (with a recorded reason), and for transfers —
`dispute`, `release-dispute`, `cancel`, `complete`.

`complete` is an override of the **acceptance** step only. The capacity gates still run: an Admin
cannot force an oversized project onto an account that cannot hold it.

Disputes stay manual. `DISPUTED` freezes the transfer — the recipient cannot push it through — and
the current authoritative owner is preserved until an Admin releases or cancels it. Nothing infers
ownership from email similarity, QR possession, project access, or the beneficiary field.

## 14. Authorization model

| Action | Server-side rule |
|---|---|
| Initiate transfer | `user_can_transfer_project` (current owner, or managing vendor who is BUSINESS_VENDOR) — else 404 |
| Accept / retry | requester must be `transfer.to_user_id` — else 404 |
| Reject | recipient only |
| Cancel | `from_owner_user_id` or `initiated_by_user_id`, or an Admin |
| Submit claim | any verified logged-in user, but never the current owner; rate limited |
| Respond to claim | `user_can_respond_to_claim` (owner/managing vendor, never the claimant) |
| Withdraw claim | claimant only |
| Admin review (read) | `admin.ownership.view` |
| Admin resolution | `admin.ownership.manage` |

Object enumeration: `_transfer_for_party()` and `_claim_for_party()` re-derive party membership from
the row on every request and `abort(404)` otherwise — a valid session plus a guessed numeric id gets
nothing. Covered by `test_transfer_and_claim_ids_cannot_be_enumerated`, which also asserts that being
the *sender* does not let you accept your own outgoing transfer.

Permission codes added: **`admin.ownership.view`** and **`admin.ownership.manage`**, following the
existing `admin.reports.view` / `admin.reports.manage` convention exactly, granted to both `admin`
and `superadmin` (same as reports). Neither is in `HIGH_IMPACT_PERMISSIONS` — matching how reports
are treated, since neither deletes content or moves money.

## 15. Audit trail

Two layers, both reusing existing architecture.

1. **Per-row governed-transition trail** in `metadata_json` (transfers already had the column; claims
   got it in `a9d3c7e1b502`), written by `_record_ownership_event()`. Every entry carries: action,
   UTC timestamp, resulting status, actor user id **or** actor admin id, and the action-specific
   detail — project id, source owner, destination owner, moved bytes, retained manager vendor,
   linked transfer id, reason/note. The trail is bounded to the last 40 events per row.
2. **`AdminActivity`** via the existing `log_admin_activity()` for every Admin action:
   `ownership_claim_review` and `ownership_transfer_review`, each recording the row id, project id,
   previous → new state, the action taken, the from/to user ids, and the Admin's reason.

`AdminActivity.admin_id` is NOT NULL, so user-initiated transitions cannot live there — that is
exactly why the per-row trail exists rather than a new user-audit table. No secrets, no credentials,
no filesystem paths appear in either layer.

## 16. Account conversion safeguards

No conversion HTTP route existed and none was added — confirmed by audit. Only the validation
foundation was implemented, as scoped:

`can_convert_to_individual(user) -> (ok, reason)` blocks a BUSINESS_VENDOR → INDIVIDUAL downgrade
while any of these hold: projects still list the account as `manager_vendor_user_id`; any active
transfer (`PENDING_ACCEPTANCE` / `PENDING_CAPACITY` / `DISPUTED`) names the account as sender,
recipient or initiator; any active claim touches a project the account owns or manages, or was filed
by the account. INDIVIDUAL accounts are never blocked by this rule. It is a pure predicate — it
deletes nothing and changes nothing either way.

INDIVIDUAL → BUSINESS_VENDOR needs no safeguard here: it is a non-destructive `account_type` flip,
and nothing in this wave touches projects, media, QR, purchases, the storage ledger or history.

**Deferred (explicit): the account-conversion HTTP/UX surface.** See §21.

## 17. HTTP routes added / changed

Audited first — no route in this area existed before this wave; nothing was duplicated. All
state-changing routes are POST-only, all are CSRF-protected by the app-wide `CSRFProtect` (no new
`@csrf.exempt` anywhere), and all re-derive authorization from the database row.

**User / vendor**

| Method | Path | Endpoint |
|---|---|---|
| GET | `/ownership` | `ownership_center` |
| POST | `/projects/<int:project_id>/transfer` | `start_project_ownership_transfer` |
| POST | `/ownership/transfers/<int:transfer_id>/accept` | `accept_ownership_transfer_route` |
| POST | `/ownership/transfers/<int:transfer_id>/retry` | `accept_ownership_transfer_route` (same rule set — retry *is* re-acceptance of the same row) |
| POST | `/ownership/transfers/<int:transfer_id>/reject` | `reject_ownership_transfer_route` |
| POST | `/ownership/transfers/<int:transfer_id>/cancel` | `cancel_ownership_transfer_route` |
| POST | `/projects/<int:project_id>/ownership-claim` | `submit_project_ownership_claim` (rate limited) |
| POST | `/ownership/claims/<int:claim_id>/respond` | `respond_ownership_claim_route` |
| POST | `/ownership/claims/<int:claim_id>/cancel` | `cancel_ownership_claim_route` |

`GET /ownership` exists because a transfer **recipient** does not manage the project yet and
therefore cannot reach the project detail page — without it they would have nowhere to accept from.

**Admin**

| Method | Path | Permission |
|---|---|---|
| GET | `/admin/ownership` | `admin.ownership.view` |
| POST | `/admin/ownership/claims/<int:claim_id>/approve` | `admin.ownership.manage` |
| POST | `/admin/ownership/claims/<int:claim_id>/reject` | `admin.ownership.manage` |
| POST | `/admin/ownership/transfers/<int:transfer_id>/<action>` (`dispute` / `release-dispute` / `cancel` / `complete`) | `admin.ownership.manage` |

No existing route was modified.

## 18. Concurrency / idempotence strategy

The same established pattern as Wave 1's `_reserve_project_quota_atomic` /
`_atomic_increment_user_counter` and Wave 3's `reserve_account_storage`: **one conditional UPDATE
gated on the row's current status**, so a concurrent or duplicated request matches zero rows and
no-ops.

`_transition_ownership_row(model, record, from_statuses, to_status, **columns)` issues
`UPDATE ... WHERE id = :id AND status IN (:from_statuses)` and returns `True` only for the caller
that changed exactly one row. Python attributes are synced **only** for the winner, so a loser never
reads a state it did not cause. `_transition_transfer` / `_transition_claim` are thin wrappers.

In `accept_project_ownership_transfer()` the gate sits between capacity reservation and the effects:

1. storage check (reserves nothing),
2. `_reserve_project_quota_atomic(recipient)` (itself atomic),
3. **gate**: `... WHERE status IN ('PENDING_ACCEPTANCE','PENDING_CAPACITY')` → `COMPLETED`,
4. only the winner runs `_release_project_quota_atomic(sender)`,
   `set_project_current_owner()` and `move_project_storage_ownership()`.

A loser at step 3 hands back the slot it reserved at step 2 and returns the winner's state untouched
— so two concurrent acceptances never double-decrement the sender, double-increment the recipient,
move MediaObjects twice, consume a duplicate recipient slot, or leave an inconsistent owner. Steps
3–4 are one DB transaction; any exception rolls the whole thing back.

An already-`COMPLETED` transfer returns unchanged rather than re-processing. Claim resolution uses
the same gate, so repeated approve/reject cannot produce a second ownership transition.

There is also a defence-in-depth guard: acceptance refuses if the project's current owner no longer
matches `transfer.from_owner_user_id`.

## 19. Focused test results

New: `tests/integration/test_wave4_vendor_ownership_backend.py` — **27 tests, 27 passed**.

Coverage map:

- *Transfer* — vendor-created customer-project transfer; creator preserved / current owner changed /
  manager vendor retained only when explicit / beneficiary preserved; no auto-transfer from
  beneficiary or creator; unauthorized initiation and unauthorized acceptance rejected; recipient
  decline; sender cancellation; expired transfer refused.
- *Capacity* — both dimensions evaluated together; project-capacity insufficient → PENDING_CAPACITY
  with the failing dimension recorded; storage insufficient → PENDING_CAPACITY with no slot consumed;
  recipient later gains capacity → same row completes; sender's slot freed only on completion;
  recipient's slot consumed only on completion; no partial storage/account movement on failure.
- *Storage* — MediaObject responsibility moves exactly once; total bytes preserved; source usage
  decreases; recipient usage increases; physical files still on disk; no duplicate ledger movement.
- *Claims* — submission; duplicate/open de-duplication; claim does not auto-transfer; vendor accept
  opens a transfer; vendor refuse escalates to Admin review; unauthorized vendor and self-response
  rejected; Admin approval; Admin rejection; approved claim still respects capacity and stays
  PENDING_CAPACITY when the recipient is full; repeated Admin resolution safe; dispute preserves the
  current owner until manual resolution.
- *Security* — unrelated user rejected on every transfer/claim mutation; transfer and claim id
  enumeration returns 404; sender cannot self-accept; normal user cannot reach Admin resolution; CSRF
  required for form mutation; GET on a mutation returns 405; permission codes present for both roles.
- *Idempotence* — repeated acceptance never double-accounts; the status gate rejects a transition
  whose row already moved on; owner-changed-underneath guard.
- *Account conversion* — vendor downgrade blocked by managed projects, then by an active transfer,
  then by an open claim; allowed once all three clear; INDIVIDUAL never blocked.

Narrowly related existing suites re-run to prove no regression:

| Suite | Result |
|---|---|
| `tests/integration/test_domain_ownership_foundation.py` | 11 passed |
| `tests/integration/test_wave3_storage_accounting.py` + `test_domain_commercial_capacity_and_reporting.py` | 84 passed |
| `tests/migrations/test_migrated_schema_lane.py` | 9 passed, 3 skipped (PostgreSQL-only), 1 **pre-existing** failure |
| `tests/gate_jr/test_v11_commercial_ownership_ux.py` | `58 passed (Agent 2's ownership/commercial UX lane, unaffected)` |

**Pre-existing failure, not a Wave 4 regression:**
`test_migrated_schema_lane.py::test_migrated_schema_still_rejects_an_invalid_addon_type` fails
identically on the unmodified starting tree (verified by stashing all Wave 4 changes and re-running).
It is a SQLite CHECK-constraint enforcement issue in the addon_catalog lane, unrelated to ownership.

## 20. Tests deliberately NOT run

Per this checkpoint's explicit policy:

- The **full suite** (`python -m pytest -q`) — not run.
- The **full PostgreSQL certification lane** — not run. The PostgreSQL-only migration tests skipped
  cleanly because `SCANSTORY_QA_DATABASE_URL` was not set, which is the intended behaviour here.
- Scanner / gate-jr device lanes, performance lanes, and every suite unrelated to ownership,
  capacity, storage or migrations.

The project lead runs the integrated focused gate, the PostgreSQL gate, and one serial full
regression at the combined checkpoint.

## 21. Known limitations / deferred work

Explicitly deferred, not silently dropped:

1. **Account-conversion HTTP/UX.** `can_convert_to_individual()` is implemented and tested; there is
   still no conversion route or page in the build. Building one was out of scope (it would be a broad
   account-management change). A future checkpoint should call this predicate from that route rather
   than re-deriving the rule.
2. **Agent 2's obsolete "not available from here yet" copy** in `templates/user/project_preview.html`
   still says transfers cannot be started from that page. That is now untrue, but rewriting that
   panel is the next Agent 2 UI checkpoint's job, per this wave's brief. The new page at `/ownership`
   is functional and does not contradict it.
3. **`templates/user/ownership.html` is functional, not designed.** It is a plain, accessible page
   using the existing design-system stylesheet. Agent 2 should restyle it (or fold its forms into the
   project detail panel) — the routes are stable and take ordinary form fields.
4. **Transfer expiry is lazy, not scheduled.** A transfer flips to `EXPIRED` when someone attempts to
   accept it after `expires_at`; there is no background sweeper. No scheduler exists in this build and
   adding one was out of scope.
5. **`VENDOR_NOTIFIED` and `response_deadline_at` are unused.** They exist in the schema but the
   current flow goes `OPEN` → vendor response directly (`vendor_notified_at` is stamped on response).
   Wiring a notification deadline would need the scheduler in (4).
6. **The concurrent-loser slot release is proven by construction, not by a threaded test.** The gate
   primitive is tested directly (`test_status_gate_rejects_a_transition_whose_row_already_moved_on`)
   and repeated acceptance is tested end to end, but no test spawns two real threads racing on one
   transfer. Wave 3 has a threaded storage-concurrency test that could be adapted if the lead wants
   that specific coverage.
7. **Coverage carry-over on transfer was not touched.** `TRANSFER_CARRY_OVER` remains governed
   entirely by the Wave 2 coverage resolver; nothing in this wave grants, extends or revokes project
   service coverage, so a transfer cannot accidentally confer perpetual hosting.
8. **Notifications are minimal and best-effort.** Six events send plain inline HTML via the existing
   `send_email_smtp` (transfer requested / accepted / declined, claim submitted, vendor responded,
   and the claimant's response notice). No Admin-resolution email — the Admin acts in the panel and
   the outcome reaches the parties through the transfer notifications. Every send is wrapped in
   try/except and runs **after** commit, matching the existing `send_payment_success_email` pattern
   exactly; no new SMTP infrastructure, no new templates, and transactional correctness never depends
   on delivery.

## 22. Merge risk

**Low.**

- `models.py`: one additive nullable column. No relationship, index or constraint changes.
- Migration: linear from the current head, additive, guarded, reversible.
- `app.py`: mostly new functions and new routes. The only rewritten function is
  `accept_project_ownership_transfer()`, whose external contract is unchanged — the 11 existing
  `test_domain_ownership_foundation.py` tests and the 84 Wave 3 / capacity tests pass untouched.
  `approve_project_ownership_claim_by_admin()` keeps its `(claim, transfer)` return shape.
  `_release_project_quota_atomic()` is new; it replaces one inline non-atomic decrement.
- Templates: two new files plus one four-line sidebar insert. `project_preview.html`,
  `projects.html`, `dashboard.html` and every other existing template are untouched, so Agent 2's
  in-flight UI work cannot conflict.
- No status vocabulary changes, so nothing downstream that switches on transfer/claim status needs
  updating.
- Conflict surface with Agent 2 is essentially the sidebar file and, if they also add routes, the
  route table region of `app.py`.

## 23. Git status

```
 M app.py
 M models.py
 M templates/admin/_sidebar_links.html
?? SCANSTORY_V1_1_WAVE4_AGENT1_VENDOR_OWNERSHIP_BACKEND_REPORT.md
?? migrations/versions/a9d3c7e1b502_ownership_claim_audit_metadata.py
?? templates/admin/ownership.html
?? templates/user/ownership.html
?? tests/integration/test_wave4_vendor_ownership_backend.py

(all Wave 4 files committed; working tree clean after the commit)
```

`git diff --check`: `clean (no output)`

## 24. Scanner files untouched — confirmation

`git diff --stat 0a5011c -- scanner_runtime.py media_processing.py compatibility_resolver.py
static/js/scanner-runtime.js templates/user/scanner.html` returns **empty output**: all five files
are byte-identical to the starting commit.

No ORB / homography / RANSAC / optical-flow / camera-behaviour / target-tracking / reacquisition /
fallback / threshold / smoothing code was read for modification or altered in any way. This wave is
purely ownership, capacity and storage accounting.

## 25. Published V1 refs untouched — confirmation

No branch, tag or ref other than the working branch `agent/v1.1-platform-admin` was created, moved,
deleted or checked out. Specifically untouched:
`release/scanstory-v1-server`, `hardening/saas-v1-production`, `v1.0.0-rc1`, `v1.0.0-rc2`.

`F:\ScanStory-main\ScanStory-integration` was never accessed. Nothing was pushed. No secrets were
printed, logged, staged or committed.
