# ScanStory V1.1 — P0 Production Hardening Report

Wave scope: the three confirmed release blockers from the completed
production-readiness audit, plus the two explicitly approved adjacent fixes.
Nothing else from that audit (19 HIGH / 27 MEDIUM, UX/responsive/accessibility)
was touched.

---

## 1. Starting HEAD

`deab3253011f87155115c656507d689026adad13` (post-sync).

The worktree began at `684a7542485377e972801acafc63410c4be95fc9` on branch
`agent/v1.1-platform-admin`, verified clean. `HEAD` was confirmed to be an
ancestor of the authoritative integration head, so the sync was a clean
fast-forward (`git merge --ff-only deab325`) with **no conflicts**. The three
commits pulled in were:

| Commit | Subject |
|---|---|
| `4107a2a` | fix(v1.1): reconcile Wave 4 and Wave 5 admin project context |
| `bdcd9fb` | test(v1.1): align migration certification with Wave 3 and Wave 4 |
| `deab325` | test(v1.1): align stale certification contracts with current behavior |

No untracked local-only artifacts were present in this worktree after the sync,
and none were created outside the deliverables listed below.

## 2. Ending HEAD

`97cedd2` — the final **code** commit of this wave. Following the repo's
existing wave convention (`docs(v1.1): record the Wave N ending commit`), the
`docs(v1.1): report P0 production hardening` commit that carries this file is
the branch tip and sits one commit above it.

Commits made, in order:

| Commit | Subject |
|---|---|
| `f938a4b` | fix(v1.1): add recoverable refund reconciliation |
| `d847676` | fix(v1.1): preserve ownership history across project deletion |
| `2b815cb` | fix(v1.1): normalize PostgreSQL runtime driver configuration |
| `97cedd2` | fix(v1.1): close dependency and admin CSRF gaps |
| *(tip)* | docs(v1.1): report P0 production hardening |

Nothing was pushed.

## 3. Files changed

| File | Change |
|---|---|
| `app.py` | Refund recovery engine + `flask reconcile-refunds` CLI + narrow admin recover API; idempotency-replay false-success fix; out-of-band refund webhook correlation; project-deletion lifecycle guard and ownership-history detachment; PostgreSQL URL normalization wired into both config sites |
| `models.py` | `ProjectOwnershipTransfer` / `ProjectOwnershipClaim`: `project_id` relaxed to nullable, `historical_project_id` + `historical_project_name` added |
| `core/config.py` | New `normalize_database_url()` (+ `POSTGRES_DRIVER_URL_PREFIX`) |
| `requirements.txt` | `requests<=2.34.2` declared as a direct dependency |
| `templates/admin/edit_admin.html` | CSRF token added to the admin delete form |
| `templates/admin/ownership.html` | 4 lines: detached history rows still render their project identity |
| `migrations/versions/c1a7f3d95e24_ownership_history_survives_project_delete.py` | **new** migration |
| `tests/integration/test_v11_p0_refund_recovery.py` | **new** focused refund-recovery tests (P0-1) |
| `tests/integration/test_v11_p0_project_delete_history.py` | **new** focused project-delete/ownership-history tests (P0-2) |
| `tests/integration/test_v11_p0_config_and_gaps.py` | **new** focused PostgreSQL-URL tests (P0-3) + both adjacent fixes |
| `tests/migrations/test_ownership_history_delete_migration.py` | **new** focused migration tests |
| `V1_1_P0_PRODUCTION_HARDENING_REPORT.md` | **new** this report |

`scanner_runtime.py`, `static/js/scanner-runtime.js` and all
recognition/detection/tracking/OpenCV geometry were not opened and not modified.

## 4. Migration revision / parent / resulting head

| | |
|---|---|
| Revision | `c1a7f3d95e24` — *ownership history survives project delete (V1.1 P0-2)* |
| Parent (`down_revision`) | `a9d3c7e1b502` — verified to still be the real head after the sync |
| Resulting head | `c1a7f3d95e24` — **single linear head**, asserted by `flask db heads` and by a test |

Upgrade: `project_id` becomes nullable on `project_ownership_transfers` and
`project_ownership_claims`; `historical_project_id` (indexed `INTEGER`) and
`historical_project_name` (`VARCHAR(255)`) are added to both. Additive and
non-destructive — no row deleted, no column dropped or retyped, no invented
backfill. Column adds are guarded by an inspector check so a partially applied
upgrade is safe to re-run.

Downgrade: re-tightens `project_id` to `NOT NULL` and drops the two columns —
but **refuses with a clear error if any detached audit row exists**
(`project_id IS NULL`), because re-tightening would otherwise require deleting
ownership evidence. The refusal is checked for *both* tables before anything is
dropped, so an aborted downgrade leaves the schema untouched. No old published
migration was edited.

## 5. Refund blocker root cause

Verified against the current code, not taken on trust from the audit text.

1. **The false success.** `initiate_admin_refund()` looked up
   `PaymentRefund.query.filter_by(idempotency_key=...)` and, if *any* row came
   back, returned a flat `{"success": True, "replay": True}` — **regardless of
   that row's status**. Replaying a refund whose provider call had FAILED
   therefore answered "success" while no money had moved. The
   `IntegrityError` fallback path (a *different* idempotency key racing the
   same source) returned the same flat success.
2. **No retry path at all.** `refund_eligibility_for_payment_order` /
   `_for_addon_purchase` return `REFUND_PREVIOUSLY_FAILED` (ineligible) for an
   existing `REFUND_FAILED` row, and the four `UniqueConstraint`s on
   `payment_refunds` (`payment_order_id`, `addon_purchase_id`,
   `provider_refund_id`, `idempotency_key`) correctly prevent a second refund
   record. Between them there was no way for an operator to re-drive a stuck
   refund on the record that already existed.
3. **No operator surface.** There was no refund-specific CLI. `reconcile-storage`,
   `reconcile-payment-activations`, `webhook-events-status` etc. all existed;
   refunds had only the read-only `GET /admin/api/refunds` list.
4. **Terminal-but-inconsistent states had no owner.** `REFUNDED` +
   `reconciliation_status=FAILED` (provider paid, local entitlement bookkeeping
   failed — set in the `except` branch of `initiate_admin_refund`) and
   `REFUNDED` + `MANUAL_REVIEW_REQUIRED` were both reachable and neither had a
   re-drive path.
5. **Out-of-band refunds were dropped.** `_process_refund_webhook_event()`
   finalized any refund webhook with no local `PaymentRefund` as
   `failure_code="unknown_refund"` — indistinguishable from genuine noise, even
   when the `payment_id` unambiguously matched a local `PaymentOrder` or
   `AddonPurchase`.

## 6. Refund recovery implementation

**Governing rule (stated in the code): the provider is the only authority on
whether money moved.** Every mutating outcome is derived from a provider *read*
first; a read that cannot be completed produces `unresolved` rather than a
second refund call.

### Case handling — `recover_payment_refund(refund, admin=None, apply_changes=False)`

| Local state | Action | Provider refund call? |
|---|---|---|
| `REFUNDED` + `APPLIED` | `already_settled`, no writes (idempotent) | No |
| any + `MANUAL_REVIEW_REQUIRED` | `manual_review`, reported only, never auto-resolved | No |
| `REFUNDED` + `FAILED`/`PENDING` | Re-drive **local reconciliation only** via `_apply_refund_reconciliation` | **No** — money already moved |
| `REFUND_FAILED` / `REFUND_REQUESTED`, provider has a matching refund | `adopted_provider_state` — adopt the provider's status onto the same row | No |
| `REFUND_FAILED` / `REFUND_REQUESTED`, provider has **no** refund on that payment | `retried` — re-attempt on the **same record** | Yes, once |
| `REFUND_PROCESSING`, provider has no matching refund | `unresolved` — the contradiction is exactly what could double-refund | **No** |
| Provider list ambiguous (partial refunds, 2+ refunds, amount mismatch) | `unresolved` | No |
| Provider read raised | `unresolved`, row untouched | No |
| Provider re-attempt raised | `retry_failed`, row back to `REFUND_FAILED`, **entitlements untouched** | Attempted, failed |

Design points worth naming:

- **The existing row is always reused.** No second `PaymentRefund` is ever
  created and none of the four uniqueness constraints was weakened.
- **Entitlements are only ever reversed by `_apply_refund_reconciliation`,**
  which is only reachable from `mark_refund_provider_result` once the provider
  reports `processed`. Provider failure structurally cannot reverse anything.
- **The row is deliberately NOT pre-marked `REFUND_PROCESSING` before a retry.**
  If the process dies mid-call the row stays as it was, and the next recovery
  run re-reads the provider and either adopts the refund that landed or retries.
  Pre-marking would have stranded it in the manual-review branch permanently.
- **No media deletion** exists on any path here; a dedicated test asserts the
  media tree is byte-identical across a full refund recovery.
- **Post-refund storage overage stays allowed.** `ACCOUNT_STORAGE` reversal is a
  negative ledger row only (unchanged Wave 3 behaviour) — existing content keeps
  working, only new consumption is blocked.

### Operator surface — `flask reconcile-refunds`

Read-only by default. `--apply` mutates; `--dry-run` and `--apply` are mutually
exclusive. Narrowing via `--refund-id <id>` and
`--source payment_order|addon_purchase`. Reports: failed provider attempts,
requested/processing, refunded-with-unapplied-reconciliation, manual review,
per-outcome counts, recovered, unresolved, errors, and out-of-band provider
refunds with no local record. **Exits non-zero** when anything is unresolved,
errored, or out-of-band, so a scheduled run cannot look clean while money is
stuck. No provider payload, exception text or credential is ever echoed.

### False-success fix

`_refund_replay_response()` now decides the replay answer from the row's
authoritative state:

- `REFUND_FAILED` → `success: False`, `code: REFUND_PREVIOUSLY_FAILED`, plus the
  real refund payload and a pointer to recovery.
- `REFUNDED` + `APPLIED` → unchanged `success: True, replay: True` (**normal
  idempotent replay is not regressed**; a test pins this).
- `REFUNDED` + `FAILED`/`PENDING` → `success: True` (money *did* move) with
  `code: REFUND_RECONCILIATION_INCOMPLETE`.
- `REFUND_REQUESTED`/`REFUND_PROCESSING` → `success: True` with
  `code: REFUND_ALREADY_PROCESSING`.

Both the idempotency-key hit and the `IntegrityError` same-source fallback route
through it. The admin refund routes already map `success: False` to HTTP 409.

### Out-of-band provider refunds

`_commercial_source_for_provider_payment()` correlates a webhook's `payment_id`
to a local `PaymentOrder` or `AddonPurchase` — **deterministically only**: it
returns nothing unless exactly one local source owns that payment id. On a
match, the event is finalized with
`failure_code = "out_of_band_refund_no_local_record"` **and the correlated
source id linked on the event row**, so it is visible, queryable and listed by
the CLI. It deliberately does **not** fabricate a `PaymentRefund`:
`requested_by_admin_id` is the record of who authorised the refund, and
inventing an admin there would corrupt the audit trail; reversing entitlements
off a dashboard action is a business decision no webhook may take. Uncorrelatable
events still report `unknown_refund`.

### Narrow admin API

`POST /admin/api/refunds/<id>/recover`, permission `admin.payments.refund` (the
same permission as issuing a refund, because it can result in a provider call).
Read-only unless `apply: true`. Returns the recovery result plus the refund
payload; 409 when the outcome is unresolved. **No new admin UI was built** in
this wave.

### Audit / logging

Admin-triggered recovery writes an `AdminActivity` row (`refund_recovery`); CLI
recovery writes a structured operator log line (ids and outcomes only, never
provider payloads). `PaymentRefund.status` / `reconciliation_status` /
`failure_message_safe` remain the authoritative fields. Provider exception text
is captured only by `app.logger.exception` and never reaches a response, a
stored message, or CLI output — asserted by tests using a poisoned exception
string.

## 7. Project deletion FK/history root cause

`ProjectOwnershipTransfer.project_id` and `ProjectOwnershipClaim.project_id` were
`nullable=False` with a plain FK to `projects.id`, and the `Project` backrefs
carried **no cascade rule**. On `db.session.delete(project)` SQLAlchemy's default
behaviour is to null out the dependent rows' FK — which immediately violates
`NOT NULL`. Every one of the four delete paths funnels through the single helper
`_delete_project_files_and_rows()`, so all four were equally broken: user delete,
admin delete, admin-own-project delete, and the dev-fixture cleanup CLI. The
failure was **status-independent** — a project whose only ownership history was a
`COMPLETED` transfer could not be deleted at all.

## 8. Runtime reproduction evidence

Reproduced before designing anything, with a disposable test on the existing
SQLite test infrastructure (no production or user data touched). Both the helper
and the HTTP route were exercised.

**Before the fix:**

```
TRANSFER COMPLETED:         RAISED IntegrityError: NOT NULL constraint failed: project_ownership_transfers.project_id
TRANSFER CANCELLED:         RAISED IntegrityError: NOT NULL constraint failed: project_ownership_transfers.project_id
TRANSFER PENDING_ACCEPTANCE:RAISED IntegrityError: NOT NULL constraint failed: project_ownership_transfers.project_id
CLAIM TRANSFER_COMPLETED:   RAISED IntegrityError: NOT NULL constraint failed: project_ownership_claims.project_id
CLAIM REJECTED:             RAISED IntegrityError: NOT NULL constraint failed: project_ownership_claims.project_id
CLAIM OPEN:                 RAISED IntegrityError: NOT NULL constraint failed: project_ownership_claims.project_id
HTTP user_delete_project -> 0   (exception propagated; 500 in a real request)
```

**After the fix:**

```
TRANSFER COMPLETED:         DELETE SUCCEEDED
TRANSFER CANCELLED:         DELETE SUCCEEDED
TRANSFER PENDING_ACCEPTANCE:RAISED ProjectDeletionBlocked: This project has an ownership transfer in progress. Complete or cancel the transfer before deleting the project.
CLAIM TRANSFER_COMPLETED:   DELETE SUCCEEDED
CLAIM REJECTED:             DELETE SUCCEEDED
CLAIM OPEN:                 RAISED ProjectDeletionBlocked: This project has an unresolved ownership claim. Resolve the claim before deleting the project.
HTTP user_delete_project -> 302  (flash + redirect, no 500)
```

The disposable reproduction file was deleted; its cases are now permanent
assertions in `tests/integration/test_v11_p0_project_delete_history.py`.

## 9. Ownership-history deletion policy

**Historical rows are preserved, never cascade-deleted.** They are ownership
evidence and outlive the project they describe.

- `_detach_ownership_history(project_id, project_name)` runs inside
  `_delete_project_files_and_rows` immediately before the project row is
  removed. One bulk `UPDATE` per table copies the project's identity into
  `historical_project_id` / `historical_project_name` and clears `project_id`.
  It is idempotent — a re-run finds no rows still pointing at the project, so a
  retried delete cannot overwrite a previously captured snapshot.
- Detached rows stay **queryable by project id** (indexed
  `historical_project_id`) and human-readable without a `projects` row to join
  to. The admin ownership screen renders the retained name and id instead of a
  bare "(deleted)".

**Active-workflow guard.** `project_deletion_block_reason(project)` refuses hard
deletion while any active transfer (`PENDING_ACCEPTANCE`, `PENDING_CAPACITY`,
`DISPUTED`) or active claim (`OPEN`, `VENDOR_NOTIFIED`, `PENDING_ADMIN_REVIEW`,
`APPROVED_BY_VENDOR`) exists, returning a finished, safe sentence containing no
other user's identity, no internal state name and no filesystem path.

- The guard is enforced **inside the shared helper** (raising
  `ProjectDeletionBlocked`), so none of the four callers can bypass it.
- It runs **first — before any unlink and before any storage credit** — so a
  refused delete leaves media and storage accounting exactly as they were.
- The three HTTP routes also check it up front and answer with a flash +
  redirect rather than an exception; the dev-fixture cleanup CLI converts it to
  a `ClickException` rather than force-resolving a live workflow.
- Archive/deactivate (`project.is_active`) remains available and unchanged.

**Unchanged by design:** physical media deletion, the "storage is credited only
after the physical delete succeeded" ordering, pair deletion, upload-session
retention, and claim→transfer semantics (a claim still never auto-transfers
ownership).

## 10. PostgreSQL URL/driver root cause

The project declares `psycopg[binary]<=3.2.3` (psycopg **v3**) and does not ship
psycopg2. Configuration validation called
`core.config.database_backend_name()`, i.e. `make_url(url).get_backend_name()`,
which returns `"postgresql"` for a **bare** `postgresql://…` URL — so validation
passed. SQLAlchemy then resolves a bare URL to its *default* PostgreSQL DBAPI,
which is psycopg2. Confirmed at runtime:

```
'postgresql://u:p%40ss@h:5432/db?sslmode=require'  -> backend=postgresql  driver=psycopg2   # passed validation, wrong driver
'postgresql+psycopg://u:p@h/db'                    -> backend=postgresql  driver=psycopg
'postgresql+psycopg2://u:p@h/db'                   -> backend=postgresql  driver=psycopg2   # passed validation, driver absent
'postgres://u:p@h/db'                              -> NoSuchModuleError                     # generic "not a valid URL" error
```

So a correct-looking managed-provider URL passed startup validation and then
failed at first connect with a module-not-found error, and the Heroku/Render
style `postgres://` (which many managed providers still emit) was rejected with
a message that named neither the cause nor the fix.

## 11. Normalization/validation behaviour

`core.config.normalize_database_url()` rewrites **only the scheme**, by string
surgery on the part before `://`. The credential/host/port/path/query remainder
is passed through byte for byte, so a percent-encoded password cannot be
decoded, re-encoded or corrupted in transit.

| Input | Result |
|---|---|
| `postgresql://…` | → `postgresql+psycopg://…` |
| `postgres://…` | → `postgresql+psycopg://…` (managed-provider form now accepted) |
| `postgresql+psycopg://…` | unchanged |
| `postgresql+psycopg2://…` | **RuntimeError** naming the driver and the fix |
| `postgresql+asyncpg://…`, `+pg8000://…` | **RuntimeError**, same shape |
| `sqlite:///…`, `mysql+pymysql://…`, `""`, `None`, non-URL text | unchanged |

Verified round-trip on a hostile URL
(`postgresql://us%40er:p%40ss%2Fword@db.example.com:6543/appdb?sslmode=require&connect_timeout=5`):
driver `psycopg`, username `us@er`, password `p@ss/word`, host, port `6543`,
database `appdb` and both query parameters all survive intact.

Wired at both configuration sites in `app.py`: inside
`_validate_required_runtime_config()` (**before** the backend check, so the gate
sees the same URL the engine is built from and an unsupported driver fails
startup with a named reason instead of at first connect) and at the
`database_uri` assignment that feeds `SQLALCHEMY_DATABASE_URI`. Alembic inherits
it automatically — `migrations/env.py` derives its URL from the live app engine.
`TEST_DATABASE_URL` is normalized on the same path, so the PostgreSQL
certification lane benefits too, while SQLite dev/test support is untouched.

**No credentials are ever printed:** error text names the driver only, and a
test asserts the password and host/database substrings are absent from the
message. **psycopg2 was not added** and no code depends on it.

## 12. `requests` dependency result

Confirmed: `app.py` imports `requests` directly (line 36, used for reCAPTCHA
verification at line ~502) and `requirements.txt` did **not** declare it — it was
riding in transitively via `razorpay`. Added
`requests<=2.34.2` (matching the file's existing `<=`-pin style and the version
already installed in the authoritative venv), with a comment recording why.
No other dependency was added, removed or upgraded; `psycopg[binary]<=3.2.3`
remains the only PostgreSQL driver declared. A test asserts `requests` is
declared and that no `psycopg2` entry exists.

## 13. CSRF fix result

`templates/admin/edit_admin.html` — the second POST form (the
`admin_delete_admin` action, ~line 256) was missing its CSRF token while the
first form at line 36 had one. Added
`<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">`. One line.
Endpoint, method, permissions and page design unchanged. Two tests added: one
proving the delete form emits the token, one proving that with CSRF enforcement
on, a POST without a token is rejected (400/403) and the admin still exists.

## 14. Focused tests run and exact pass/fail counts

Focused/scoped only — the full suite was **not** run, and the PostgreSQL
certification lane was **not** run.

| Lane | Command scope | Result |
|---|---|---|
| New P0 suite (all 3 blockers + both adjacent fixes) | `tests/integration/test_v11_p0_refund_recovery.py`, `tests/integration/test_v11_p0_project_delete_history.py`, `tests/integration/test_v11_p0_config_and_gaps.py` | **57 passed, 0 failed** |
| New migration suite | `tests/migrations/test_ownership_history_delete_migration.py` | **4 passed, 0 failed** |
| Config / migration-chain regression | `tests/integration/test_final_runtime_database_hardening.py`, `tests/migrations/test_alembic_foundation.py`, `tests/migrations/test_migrated_schema_lane.py`, `tests/ops` | **31 passed, 0 failed, 3 skipped** (PostgreSQL-only, correctly skipped without `SCANSTORY_QA_DATABASE_URL`) |
| Refund / webhook regression | `tests/integration/test_admin_refunds.py`, `tests/integration/test_razorpay_webhook_reconciliation.py`, `tests/gate_jr/test_v11_admin_refund_ux.py`, `tests/integration/test_wave5_admin_commercial_completion.py` | **97 passed, 0 failed** |
| Ownership / admin-project regression | `tests/integration/test_wave4_vendor_ownership_backend.py`, `tests/gate_jr/test_v11_commercial_ownership_ux.py`, `tests/integration/test_admin_projects_module.py`, `tests/integration/test_admin_crud_hardening.py` | **101 passed, 0 failed** |

**Totals: 290 passed, 0 failed, 3 skipped (PostgreSQL-only).**

New-suite coverage by requirement:

- **Refunds (22 tests):** failed refund retry uses the same row; provider
  failure never reverses entitlements; confirmed provider refund + failed local
  reconciliation retries local only; duplicate provider refund not issued
  (adoption path); stale idempotency replay of `REFUND_FAILED` no longer returns
  fake success; successful replay not regressed; CLI dry-run writes nothing; CLI
  apply is idempotent and exits 0 on a clean second pass;
  `MANUAL_REVIEW_REQUIRED` not auto-resolved; unreadable provider state never
  issues a refund; `REFUND_PROCESSING` with no provider record parked for
  review; refund recovery never deletes media; storage overage behaviour
  unchanged; recovery audited via `AdminActivity`; no provider exception text
  leaks; out-of-band refund correlated not dropped; uncorrelatable refund still
  `unknown_refund`; admin recover API requires `apply` to mutate.
- **Project deletion (17 tests incl. parametrisation):** historical transfer and
  historical claim deletions no longer 500; all three active transfer states
  block; all four active claim states block; blocked delete leaves media,
  pairs and storage untouched; blocked route returns a safe message not a 500;
  detached rows keep `historical_project_id`/`_name` and status; media and pair
  deletion semantics unchanged; detachment idempotent on retry; admin ownership
  page still identifies detached history.
- **PostgreSQL (14 tests incl. parametrisation):** bare `postgresql://` and
  `postgres://` normalized; `+psycopg` unchanged; `psycopg2`/`asyncpg`/`pg8000`
  rejected with a named reason and no credential leakage; credentials and query
  parameters survive; SQLite/MySQL/blank/non-URL untouched; running test app
  still SQLite; normalized URL passes the backend gate.
- **Adjacent (3 tests):** `requests` declared and no `psycopg2`; delete form
  emits CSRF token; CSRF enforcement still rejects a tokenless POST.

## 15. `git diff --check` result

Clean — exit code 0, no whitespace or conflict-marker errors. (Git emits its
usual advisory `LF will be replaced by CRLF` notice for `core/config.py` on this
Windows worktree; that is a line-ending normalization notice, not a
`diff --check` finding.)

## 16. Known limitations / manual-review paths

1. **`MANUAL_REVIEW_REQUIRED` is never auto-resolved.** Recovery reports it and
   stops. Subscription (`payment_order`) refunds always land here by existing
   product rule — subscription dates and limits are not changed automatically —
   as do `VALIDITY_EXTENSION` add-on refunds. An operator must act. Note the
   consequence: `flask reconcile-refunds` keeps exiting non-zero for as long as
   any manual-review refund exists, by design — a scheduled run must not look
   clean while a human still owes an outcome.
2. **`REFUND_PROCESSING` with no matching provider refund stays unresolved.**
   Deliberate: our record says the create call was accepted and the provider
   disagrees, and auto-re-issuing on that contradiction is the one path that
   could double-refund. A human resolves it.
3. **Ambiguous provider refund sets stay unresolved.** If the payment carries a
   partial refund, two refunds, or a full refund for a different amount, recovery
   refuses to guess.
4. **Provider reads that fail leave the row untouched.** Recovery never fabricates
   provider success; the CLI exits non-zero so this cannot pass silently.
5. **Out-of-band refunds are recorded, not reconciled.** No `PaymentRefund` row
   is fabricated and no entitlement is reversed; the correlated event is
   surfaced for an operator. Correlation is deterministic-only — if two local
   sources share a payment id, nothing is linked.
6. **Recovery is not automatic.** It requires `flask reconcile-refunds --apply`
   or an explicit admin API call. No scheduler was added this wave.
7. **No refund-recovery admin UI.** API + CLI only, by scope.
8. **Blocked deletion has no force override.** A project with an active
   transfer/claim cannot be hard-deleted at all until the workflow is resolved
   or cancelled; archive/deactivate remains the escape hatch.
9. **`historical_project_*` is populated at deletion time only.** Live rows read
   NULL there (they still have `project_id`), and no backfill was invented for
   pre-existing rows. Consumers should read
   `COALESCE(project_id, historical_project_id)`.
10. **Migration downgrade refuses once history has been detached.** By design —
    the alternative is deleting ownership evidence.
11. **`postgres://` is normalized, not blocked.** If a deployment genuinely
    intended a non-psycopg driver it now fails startup rather than silently
    picking one; that is intended but is a behaviour change for any environment
    that was relying on the old bare-URL default.

## 17. Confirmation: scanner/runtime untouched

Confirmed. `scanner_runtime.py`, `static/js/scanner-runtime.js`, and all
recognition / detection / tracking / OpenCV geometry were not opened, not read
and not modified. `git status --short` and `git diff --stat` list no file under
`static/js/` and no scanner module.

## 18. Confirmation: no integration worktree edits

Confirmed. All work was performed in
`F:\ScanStory-main\ScanStory-v1.1-agent1` on branch
`agent/v1.1-platform-admin`. `F:\ScanStory-main\ScanStory-integration` was never
written to (it was not read from either — the sync was done via a commit-ish
already present in this worktree's object store). No published V1 branch or tag
(`release/scanstory-v1-server`, `hardening/saas-v1-production`, `v1.0.0-rc1`,
`v1.0.0-rc2`) was touched. Nothing was pushed.

## 19. PostgreSQL certification still required

**This wave is certified against SQLite only. A fresh-PostgreSQL certification
run is still required and has NOT been performed here.**

This wave adds an Alembic migration (`c1a7f3d95e24`) that relaxes a `NOT NULL`
constraint and adds indexed columns on two tables, and it changes the runtime
PostgreSQL driver resolution. Neither can be considered certified from SQLite
evidence. The human/integration workflow must still perform, separately:

- a fresh-PostgreSQL baseline → `c1a7f3d95e24` upgrade,
- targeted schema/FK verification on `project_ownership_transfers` and
  `project_ownership_claims` (nullability, the new columns, the
  `ix_*_historical_project_id` indexes, and that the existing FKs to
  `projects.id` behave as intended under a real delete),
- a real connect against `postgresql+psycopg://` proving the normalized URL
  drives psycopg v3,
- the PostgreSQL-focused test lane (the 3 tests skipped above,
  `SCANSTORY_QA_DATABASE_URL` set to a **disposable** database),
- and the final full regression.

No existing integration QA database was mutated; no PostgreSQL QA credentials
were available in this environment and none were sought.

## 20. Final verdict

**P0 COMPLETE — READY FOR POSTGRESQL CERTIFICATION**

All three confirmed release blockers are fixed with focused tests, and both
approved adjacent fixes are in. This is explicitly **not** a claim of production
readiness: section 19 applies, and the fresh-PostgreSQL certification lane and
full regression remain the human/integration workflow's job.
