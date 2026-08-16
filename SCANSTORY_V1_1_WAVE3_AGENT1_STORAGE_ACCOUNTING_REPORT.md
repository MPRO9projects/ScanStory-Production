# ScanStory V1.1 — Wave 3 / Agent 1

## Authoritative Account Storage Accounting

Wave 2 made `SubscriptionPlan.base_storage_bytes` a real entitlement number with
nothing behind it, and Agent 2's UI honestly labelled it "entitlement only,
storage usage is not tracked yet". Wave 3 is what makes that disclaimer
obsolete: a real per-file ledger, a real account usage number, real enforcement
on every path that consumes storage, and a reconciliation command that can
account for the media that already exists.

---

## 1. Starting commit

`22161837f3b43c0abee542ae19f78b0148770adf`

Branch `agent/v1.1-platform-admin`, synced from `develop/scanstory-v1.1` as a
**fast-forward** (`486c1f1..2216183`) — no conflicts, nothing to resolve.

Post-sync verification of the prerequisites this wave builds on:

| Prerequisite | Present in synced tree |
|---|---|
| `entitlements.py` with `get_effective_entitlements(user, unlimited_override)` | yes |
| `SubscriptionPlan.plan_family` / lifecycle / revision | yes |
| `SubscriptionPlan.base_storage_bytes` (BigInteger) | yes |
| Per-file media policy columns (`max_image_bytes`, `max_video_bytes`, …) | yes |
| Experience flags (`allow_direct_qr` / `allow_detect_once` / `allow_tracked_overlay`) | yes |
| Wave 2 migration `e7a3f9c2b1d5` at head | yes |
| Agent 2 reconciliation (profile/pricing/admin plan pages reading real fields) | yes |

## 2. Ending commit(s)

| Commit | Subject |
|---|---|
| `b6c44f0` | feat(v1.1): add the authoritative media storage ledger schema |
| `594f900` | feat(v1.1): resolve real account storage entitlement and usage |
| `25898ee` | feat(v1.1): enforce account storage across create, replace, delete and transfer |
| `bd86ec3` | test(v1.1): cover the Wave 3 storage accounting ledger and enforcement |
| *(this document)* | docs(v1.1): record the Wave 3 storage accounting build |

Branch `agent/v1.1-platform-admin`. Nothing pushed; nothing merged.

## 3. Files changed

| File | Status | What changed |
|---|---|---|
| `models.py` | modified | `MediaObject` ledger model; `User.storage_used_bytes`; `AddonCatalog.storage_bytes_delta`; `ACCOUNT_STORAGE` added to `ADDON_TYPES` / `ENTITLEMENT_TYPES` and the `ck_addon_catalog_type` CHECK; `EntitlementTransaction.delta_value` → BigInteger |
| `storage_accounting.py` | **new** | Ledger reads/writes, the concurrency-safe reservation primitive, the pure policy predicates, transfer primitives |
| `entitlements.py` | modified | Storage section of the central resolver: three sources, usage, remaining, overage, `storage_usage_tracked` → True |
| `app.py` | modified | Storage-key glue, create/upload enforcement, replacement enforcement, physical-delete-first freeing, `ACCOUNT_STORAGE` add-on wiring, admin grants, transfer wiring, `flask reconcile-storage` |
| `templates/admin/addons.html` | modified | `storage_bytes_delta` field on the create/edit add-on forms + the effect summary column (catalog wiring, not storage-meter UI) |
| `migrations/versions/f2b7d4e9c3a6_media_storage_ledger_and_account_storage.py` | **new** | The migration |
| `tests/integration/test_wave3_storage_accounting.py` | **new** | 38 focused behavioural tests (45 collected items) |
| `tests/migrations/test_media_storage_ledger_migration.py` | **new** | 4 focused schema/FK tests |
| `tests/integration/test_wave2_entitlement_foundation.py` | modified | one assertion updated to the delivered Wave 3 contract (§18) |
| `tests/integration/test_wave1_p0_blockers.py` | modified | one assertion updated (§18) |
| `tests/integration/test_v1_agent2_admin_parity.py` | modified | one assertion updated (§18) |
| `SCANSTORY_V1_1_WAVE3_AGENT1_STORAGE_ACCOUNTING_REPORT.md` | **new** | This document |

**Not touched:** every scanner/algorithm file (§23), every user-facing template,
every V1 branch or tag.

## 4. Schema / model

`MediaObject` → table `media_objects`. Named to match the existing convention
(CamelCase model, snake_case plural table, alongside `ProjectPair`,
`EntitlementTransaction`, `ProjectServiceCoverage`).

```
id                    Integer PK
owner_user_id         FK users.id            nullable, indexed   -- who pays
owner_admin_id        FK admins.id           nullable, indexed   -- admin-owned: nobody pays
project_id            FK projects.id         nullable, ON DELETE SET NULL
pair_id               FK project_pairs.id    nullable, ON DELETE SET NULL
media_role            String(30)   'trigger_image' | 'video'
storage_key           String(600)  root-qualified pointer, e.g. 'user/videos/12_0.mp4'
size_bytes            BigInteger   NOT NULL
counts_toward_quota   Boolean      NOT NULL DEFAULT true
status                String(20)   'ACTIVE' | 'SUPERSEDED' | 'DELETED'
source                String(20)   'upload' | 'reconciliation'
created_at            DateTime     NOT NULL
superseded_at         DateTime     nullable
deleted_at            DateTime     nullable
reconciled_at         DateTime     nullable
```

**BigInteger for every byte field.** Wave 1's audit flagged Integer's ~2.1GB
cap; `size_bytes`, `User.storage_used_bytes`, `AddonCatalog.storage_bytes_delta`
and `EntitlementTransaction.delta_value` are all BigInteger. A 5GB object
round-trips in test.

**No blobs in PostgreSQL.** Metadata and accounting only. Bytes stay exactly
where they already are — `data/images`, `data/videos`, `data_admin/*`, with the
existing `{project_id}_{pair_index}.ext` naming. `storage_key` prefixes the root
(`user/` vs `admin/`) because the two trees reuse the same filenames, so an
unqualified name is ambiguous across them. `storage.py`'s
`build_storage_key()`/`LocalFilesystemStorage` belongs to the separate,
feature-flagged Experience Creator pipeline and is deliberately not the scheme
the live upload paths use — the ledger points at the live scheme.

**FK delete behaviour — decided deliberately, not left undefined.**
`ON DELETE SET NULL` on both `project_id` and `pair_id`, the pattern Wave 1
established in `d4e8b2c6a0f3` for `upload_sessions`. Rationale: a `MediaObject`
is an accounting record. If a Project or ProjectPair vanishes through an ORM
cascade that bypasses the delete helper (`Project.pairs` has
`cascade="all, delete-orphan"`; `Admin.projects` and `User` cascade too), the
row must **survive with its references cleared** rather than be cascade-deleted
— deleting it would silently free storage for bytes that may still be on disk,
which is the exact failure mode this wave exists to prevent. The rows remain
visible to reconciliation as orphaned accounting.

**Duplicate-accounting prevention.** A partial unique index,
`uq_media_objects_active_storage_key` on `storage_key WHERE status = 'ACTIVE'`
(declared for both PostgreSQL and SQLite, so `db.create_all()` in the test
suite builds the same constraint the migration ships). At most one live row may
claim a storage path — that is what stops a reconciliation rerun double-counting
the same file. Superseded/deleted history legitimately reuses the key, because a
replacement writes the same path via `os.replace`, hence *partial* rather than a
plain unique constraint.

Also added: `User.storage_used_bytes` (BigInteger, NOT NULL, default 0) and
`AddonCatalog.storage_bytes_delta` (BigInteger, nullable).

## 5. Migration revision / head

| | |
|---|---|
| New revision | `f2b7d4e9c3a6` — *media storage ledger and account storage entitlement (V1.1 Wave 3)* |
| Revises | `e7a3f9c2b1d5` (Wave 2 head) |
| New head | `f2b7d4e9c3a6` — single-headed chain, verified in test |

Contents: create `media_objects` + its indexes; add `users.storage_used_bytes`;
add `addon_catalog.storage_bytes_delta`; widen `ck_addon_catalog_type` to permit
`'ACCOUNT_STORAGE'` (same replace-in-place technique as Wave 1's
`c3f7a1d5e9b4`, historical revisions never edited); widen
`entitlement_transactions.delta_value` to BigInteger.

**No filesystem scanning inside Alembic.** The migration creates an *empty*
table and leaves every `storage_used_bytes` at 0. Schema migration and
filesystem reconciliation are separate concerns: making a schema upgrade depend
on the media volume being mounted and complete would produce billing numbers
nobody can audit. Zero means "not yet reconciled", and
`flask reconcile-storage` — run by an operator, later, on a host that actually
has the media — populates it. **No fake default usage values anywhere.**

PostgreSQL/SQLite compatible: `server_default` on every NOT NULL column so
existing rows backfill in one ALTER; `batch_alter_table` for SQLite's
table-rebuild semantics; the `delta_value` widening is skipped on SQLite, where
INTEGER is already 64-bit and a batch rebuild would be pure risk for no effect.

## 6. Counted-media definition

**Counted** — retained, durable, customer-uploaded ScanStory media:

* trigger images (`data/images/{project}_{pair}.jpg`)
* videos (`data/videos/{project}_{pair}.mp4`)

**Not counted, and deliberately given no ledger row:**

| Excluded | Why |
|---|---|
| Generated QR PNGs (`data/qr_codes/`) | server-generated; the customer neither uploaded it nor controls its size |
| `.npz` recognition artifacts (`data/features/`) | our recognition pipeline's output |
| `_work.jpg` / `_fast.mp4` derivatives | our processing intermediates |
| `data/tmp_uploads/` chunks and staging files | ephemeral; deleted on success and on failure |
| Logs, backups, `static/` assets, app files | not customer media at all |
| Admin-owned project media | recorded with `counts_toward_quota=False` — real, retained, and tracked for deletion/reconciliation, but there is no subscriber account behind an admin project to bill |

No additional durable customer-origin file type was found that warranted
inclusion, so no new billed category was invented.

## 7. Storage ownership rule

Reuses the existing ownership helper — no second ownership system:

```python
def project_storage_owner_ids(project):
    if project.owner_admin_id:            return None, project.owner_admin_id
    return project_current_owner_user_id(project), None
```

`project_current_owner_user_id()` is Wave 1/2's helper
(`current_owner_user_id or owner_user_id`). Consequences:

* Normal project → the current owning account carries the storage.
* Transferred project → storage follows the current owner (§15).
* Vendor-managed project → the **owner** is billed, not the managing vendor.
  A vendor replacing media on a transferred project spends the owner's
  allowance, which matches who the ledger already bills.
* Admin-owned project → `owner_admin_id`, uncounted.

## 8. Storage service / helper API

New module `storage_accounting.py`. It imports models only; `entitlements.py`
imports **it** (never the reverse), so the effective allowance is always passed
*in* as a parameter — which also makes every predicate pure and directly
testable.

| Function | Purpose |
|---|---|
| `account_storage_used_bytes(user_id)` | authoritative ledger SUM (the audit number) |
| `stored_storage_used_bytes(user)` | the materialized enforcement counter |
| `project_counted_bytes(project_id)` | bytes a project carries on transfer |
| `active_media_objects(...)` / `active_media_object_for_key(key)` | ledger reads |
| `record_media_object(...)` | add an ACTIVE row (caller reserves first) |
| `supersede_media_object(obj)` | retire a row whose bytes were overwritten in place |
| `mark_media_object_deleted(obj)` | free bytes — **only after a successful physical unlink** |
| `reserve_account_storage(user_id, delta, allowance)` | the atomic conditional-UPDATE reservation |
| `release_account_storage(user_id, delta)` | give bytes back, floored at zero |
| `can_consume(used, allowance, new)` | new-consumption predicate (pure) |
| `evaluate_replacement(used, allowance, old, new)` | `(allowed, projected)` (pure) |
| `evaluate_storage_transfer(project_id, recipient_used, recipient_allowance)` | `(ok, project_bytes)` |
| `move_project_storage_ownership(project_id, from_user_id, to_user_id)` | move responsibility inside the caller's transaction |

None of the write helpers commit — they join the caller's transaction, so a
rollback releases the accounting exactly like it already releases a project slot.

Glue in `app.py` (needs the app's directory constants):
`build_media_storage_key`, `media_storage_abs_path`, `project_storage_owner_ids`,
`account_storage_state`, `record_pair_media_objects`,
`release_project_media_accounting`, `evaluate_project_storage_transfer`,
`grant_account_storage`, `reconcile_storage_ledger`.

## 9. Entitlement resolver changes

Extended the **existing** `get_effective_entitlements()` — no second resolver.
Field names follow the resolver's established style (`*_bytes`, `base_*`,
`purchased_*`, `admin_granted_*`, `effective_*`, `*_used`, `*_remaining`,
`over_*`), mirroring how project capacity and scans are already exposed.

| Key | Meaning |
|---|---|
| `base_storage_bytes` | plan allowance (unchanged from Wave 2) |
| `purchased_storage_bytes` | sum of `ACCOUNT_STORAGE` ledger rows from purchases/refunds |
| `admin_granted_storage_bytes` | sum of `ACCOUNT_STORAGE` ledger rows with `source_type='admin_grant'` |
| `effective_storage_bytes` | base + purchased + granted (None = unenforced) |
| `storage_used_bytes` | `users.storage_used_bytes` |
| `storage_remaining_bytes` | `max(0, effective - used)`, None when unenforced |
| `storage_usage_tracked` | **now `True`** |
| `over_storage` | `used > effective` |
| `storage_overage_bytes` | `max(0, used - effective)` |

The split comes from the resolver's existing `ledger_breakdown()` helper, so the
three sources stay separately auditable and neither can silently overwrite the
other — the same discipline already applied to `EXTRA_SCANS` and
`PROJECT_CAPACITY`.

`storage_usage_tracked` flipping to True is what retires Agent 2's disclaimer.
It is read by `_entitlement_summary()` and rendered as
`{% if not entitlement_summary.storage_usage_tracked %}` in
`templates/user/profile.html`, `templates/admin/view_user.html` and
`templates/admin/user_dashboard_context.html` — those blocks now simply stop
rendering. **No template was edited** (UI is a later checkpoint), but
`_entitlement_summary()` now also carries `effective_storage_display`,
`storage_used_display`, `storage_remaining_display`, `over_storage` and
`storage_overage_display` so that checkpoint has the numbers ready.

**Deliberate reading:** a NULL `base_storage_bytes` means "this plan states no
allowance", which is treated as *unenforced*, not as zero — the same reading
Wave 2's `is_downgrade()` already applies. Purchased or granted bytes on top of
an unstated base therefore also leave the account unenforced; a plan must state
a base before storage can be metered. This is what stops Wave 3 from
retroactively imposing a quota on every existing account.

## 10. Storage add-on

Canonical type: **`ACCOUNT_STORAGE`**, matching the existing constant style
(`EXTRA_SCANS`, `VALIDITY_EXTENSION`, `PROJECT_CAPACITY`,
`PROJECT_SERVICE_COVERAGE`).

Extends the existing `AddonCatalog` / `AddonPurchase` / `EntitlementTransaction`
architecture exactly as those three did — no parallel mechanism:

* `AddonCatalog.storage_bytes_delta` (BigInteger) is the canonical quantity, in
  **bytes**, alongside `scan_delta` / `validity_days_delta` / `project_delta`.
* `_addon_effect()` gains one branch: `storage_bytes_delta * quantity`.
* `ADDON_PURCHASABLE_TYPES` / `ADDON_CATALOG_EDITABLE_TYPES` include it, so it
  is creatable through `/admin/addons` and seedable through the existing
  `flask seed-addon-catalog --file` command (`storage_bytes_delta` is now a
  recognised seed field).
* `_addon_catalog_form_values()` parses and validates it, and reuses
  `_addon_effect()` as its probe — an item that would be rejected at checkout
  cannot be saved as available.

**No invented price or quantity.** No `+1GB` or `+5GB` constant exists anywhere
in the code; no default price is applied. A catalog row with no
`storage_bytes_delta` is rejected with `ADDON_INVALID` rather than defaulted
(tested). This is the same non-invented-values discipline every prior wave used.

**Three distinct sources.** `_apply_entitlement_transaction()` writes the ledger
row and — uniquely for `ACCOUNT_STORAGE` — materializes nothing, because the
effective allowance is composed at read time from plan base + purchased ledger
rows + admin-grant ledger rows. That is what keeps the sources separately
auditable, and it means purchased storage **survives upgrade, downgrade and
subscription lapse for free**: there is no re-materialization path that could
drop it.

**Refund / reversal** goes through Wave 1's existing
`_apply_refund_reconciliation()`. The negative ledger row it already writes for
any entitlement type *is* the whole reversal — an explicit
`ACCOUNT_STORAGE` branch documents that there is nothing to unwind and no media
to touch. If the reduced allowance now sits below usage the account simply
becomes over-storage: content keeps working, only new consumption is blocked
(tested end to end through `_create_refund_row_for_source` →
`_apply_refund_reconciliation`).

## 11. Admin storage grants

The existing admin-grant architecture supported storage safely, so it was
extended rather than replaced. `grant_account_storage(admin, user, delta_bytes,
reason)` mirrors `admin_grant_extra_scans` exactly:

1. `log_admin_activity(admin.id, "account_storage_grant", ...)` → the audit row.
2. `_apply_entitlement_transaction(..., source_type=ADMIN_GRANT_SOURCE_TYPE,
   source_id=activity.id)` → the ledger row, keyed to that audit row.

* **Additive** — a ledger row, never a bare `+=` on a materialized column.
* **Auditable** — an `AdminActivity` row per grant, and the ledger row's
  `source_id` points straight at it.
* **Revocable** — a negative delta writes a second, equally auditable row. It
  never deletes media; if it creates over-storage, existing content remains and
  only new consumption is blocked (tested).
* **Separate from purchases** — `source_type='admin_grant'` vs
  `'addon_purchase'`/`'refund'`, which is exactly how the resolver splits them.

HTTP surface: one route, `POST /admin/users/<user_id>/grant-storage`, guarded by
the existing `admin.users.manage` permission. No template was changed.

## 12. Upload / create enforcement

Wired into all three retained-media creation paths.

**`POST /upload` (`handle_upload`) — the main multi-pair path**

1. Per-file validation runs first, unchanged: `validate_image` / `validate_video`
   against `min(plan policy, immutable server ceiling)`.
2. Retained bytes are measured from the **validated temp files** — the bytes
   that will actually be retained, not a client-declared length.
3. **The entire retained logical set is weighed at once**, so a multi-pair
   project is accepted or rejected whole. There is no path that persists pair 1
   and then rejects pair 2 leaving orphaned accounting (tested).
4. Cheap precheck (`can_consume`) rejects before any project row exists, and
   cleans up the temp files.
5. Inside the transaction, the **authoritative atomic reservation** runs
   (§17), then the project-slot reservation, then the rows.
6. `MediaObject` rows are written in the *same* transaction as the reservation
   and the pair rows.
7. Any exception → `db.session.rollback()`, which releases the reservation, plus
   the pre-existing saved-file and temp-file cleanup. A failed upload leaves no
   permanent usage (tested).

**`POST /api/uploads/sessions/<id>/finalize` (resumable)** — same reservation,
same rollback release. `_ResumableQuotaLimitReached` now carries a safe
code/message so the storage gate reuses the existing rollback-and-report handler
(new client code `STORAGE_LIMIT_REACHED`) instead of duplicating ~25 lines.

**`POST /admin/projects/upload`** — ledger rows recorded (so deletion and
reconciliation see the media) with `counts_toward_quota=False`; no reservation,
because no subscriber account is billed.

Two independent gates, both of which must pass: a storage allowance never
substitutes for or relaxes a per-file limit, and vice versa (tested with a
500GB allowance and a 10-byte per-file video policy — still rejected).

## 13. Replacement enforcement

`POST /projects/<id>/edit` (`user_edit_project`) was restructured into two
phases, implementing the required flow exactly:

**Phase 1 — validate and decide. Nothing is written to disk, no old media is
touched, no reprocessing is scheduled.**

authenticate/authorize (`user_can_manage_project`) → resolve effective
entitlements → identify the old authoritative media (`active_media_object_for_key`)
→ read old counted bytes → validate the new file against current per-file policy
→ calculate projected logical storage → **reject before any expensive work** if
storage policy fails, removing every staged temp file.

**Phase 2 — commit the approved swaps.**

`os.replace` onto the *same* storage key is both the write and the physical
delete of the old bytes: it is atomic, and it either succeeds or leaves the old
file intact, so there is no window where accounting has been freed while the old
media survives. Then the old ledger row is **SUPERSEDED** (not DELETED — the
file at that key still exists, it just holds new content), a new ACTIVE row is
recorded with the bytes re-stat'd from disk after
`standardize_uploaded_image()`, and the net delta is applied to the counter.

Usage/allowance are read once and walked forward locally as each swap is
approved, so a multi-pair edit cannot approve two growths that only fit
individually.

**Policy implemented (`evaluate_replacement`), all cases tested:**

| Account state | Replacement | Result |
|---|---|---|
| within allowance | smaller | allowed |
| within allowance | larger, projected ≤ allowance | allowed |
| within allowance | larger, projected == allowance | allowed |
| within allowance | larger, projected > allowance | **blocked** |
| **over** allowance | smaller (strictly decreases total) | allowed — even if still over |
| **over** allowance | equal size | **blocked** |
| **over** allowance | larger | **blocked** |
| no stated allowance | anything | unenforced |

Verified unchanged by a replacement: **QR filename**, **pair count**, and the
project's grandfathered experience/playback mode — no mode change is forced as a
side effect of a media swap. A rejected replacement leaves the old file
byte-identical on disk and the counter untouched (tested).

## 14. Deletion semantics

**Physical delete first. Always.** Hooked into Wave 1's existing
`_delete_project_files_and_rows()` — the same helper P0-4 fixed — rather than a
parallel deletion path.

Order:

```
_unlink_project_media(...)  ->  failures            # physical deletion attempted
release_project_media_accounting(project_id, failures)   # THEN accounting
```

`release_project_media_accounting()` frees a row only if its file is genuinely
gone: not in `failures` **and** not still present on disk. Otherwise the row
stays `ACTIVE`, stays counted, and an operational error is logged. There is no
code path that decrements first and swallows a deletion failure.

* Successful delete → rows `DELETED`, bytes freed, and the rows **survive the
  project** (ON DELETE SET NULL) so the deletion stays auditable.
* Partial failure → the freed file's bytes are released, the locked file's bytes
  stay counted, the file stays on disk (tested with a `PermissionError` injected
  for the video only: usage drops 30 → 20, not 30 → 0).
* Retry → `release_project_media_accounting()` is idempotent and rerunnable;
  once the operator clears the lock and the file goes, a retry frees the
  remainder, and further runs are no-ops (tested).
* **Archive / suspend / deactivate frees nothing** — `is_active = False` touches
  no file and no ledger row (tested).

## 15. Transfer primitives

Built as **callable primitives, not routes.** Wave 2's report noted
ownership-transfer has service functions but no HTTP surface; that is still
true and out of scope here. No routes were added.

* `evaluate_project_storage_transfer(project, recipient)` → `(ok, project_bytes)`.
  Computes the project's counted bytes, the recipient's current usage and
  effective allowance. Takes plain objects, needs no request context, and is
  directly callable by whichever future checkpoint adds the HTTP surface.
* `move_project_storage_ownership(project_id, from_user_id, to_user_id)` moves
  every ACTIVE row's `owner_user_id` and adjusts both counters. It **joins the
  caller's transaction** rather than committing, on purpose.

Wired into the existing `accept_project_ownership_transfer()`:

* The storage check runs **before** `_reserve_project_quota_atomic()`, so an
  insufficient-storage recipient needs no counter unwound.
* Insufficient → `status = "PENDING_CAPACITY"` and return. Storage is another
  capacity dimension, not a new state, so the existing PENDING_CAPACITY
  mechanism and its resume path represent it without inventing anything. Nothing
  is deleted, no accounting moves, no ownership moves, no project slot is
  consumed, the sender stays the owner (tested).
* Sufficient → the accounting move happens in the **same transaction** as
  `set_project_current_owner()`, so ownership and storage responsibility can
  never end up split (tested).

## 16. Reconciliation command

```
flask reconcile-storage             # dry run (default)
flask reconcile-storage --dry-run   # explicit
flask reconcile-storage --apply     # persist
```

Naming and the `--apply`-defaults-to-dry-run convention match the repo's
existing `flask seed-addon-catalog` / `flask reconcile-quota-counters` commands.
Logic lives in `reconcile_storage_ledger(apply_changes)` so it is testable
without a CLI runner.

**Non-destructive by construction.** It reads the filesystem and writes
`media_objects` rows plus `users.storage_used_bytes`. It contains no `unlink`,
no `remove`, and no row deletion. Verified by a test that snapshots the media
directories, runs it twice with `--apply`, and asserts the file listing is
byte-for-byte identical.

**Deterministic, rerunnable, idempotent.** Unit of work is
`(project, pair, role)`; the dedup key is the ACTIVE `storage_key`. A rerun
finds every row it created and reports it as already-reconciled instead of
double-counting (tested).

**Anomaly reporting — nothing is guessed:**

| Situation | Behaviour |
|---|---|
| DB row, file missing | reported in `missing_files`; **no row created, no bytes fabricated** |
| File on disk, no DB row | reported separately in `orphan_files`; not counted (an orphan has no owner to bill); never deleted |
| Project with no resolvable owner | reported in `ambiguous_ownership`; skipped, never guessed |
| Ledger size ≠ disk size | reported in `size_mismatches`; corrected to disk truth under `--apply` |
| `OSError` on stat | reported in `errors` |

Report fields: discovered, created, already-reconciled, total bytes accounted,
missing files, orphan files, ambiguous ownership, size mismatches, per-account
counter drift, errors.

Finally it re-derives every `users.storage_used_bytes` from the ledger, which is
what makes the enforcement counter repairable rather than permanently drifted.

## 17. Concurrency strategy

**Chosen strategy: a materialized enforcement counter plus a single conditional
UPDATE** — deliberately the same pattern Wave 1 used for project capacity, not a
new mechanism.

`app._atomic_increment_user_counter()` solves this exact class of problem by
never reading-then-writing. `reserve_account_storage()` is its byte-valued
sibling:

```python
used = func.coalesce(User.storage_used_bytes, 0)
query = User.query.filter(User.id == user_id)
if allowance_bytes is not None:
    query = query.filter(used + delta <= int(allowance_bytes))
updated = query.update({User.storage_used_bytes: used + delta}, ...)
return updated == 1
```

**Why a materialized column at all.** `SUM(media_objects.size_bytes)` cannot be
compared and incremented atomically without a read-then-write, so — exactly as
app.py already documents for `subscribed_project_limit` — `media_objects` is the
**audit ledger** and `users.storage_used_bytes` is the **enforcement value**.
`flask reconcile-storage` re-derives the column from the ledger, so drift is
detectable and repairable rather than permanent.

**Why the allowance is a bind parameter rather than a second materialized
column.** It avoids adding a `storage_limit_bytes` column that every plan
activation, add-on fulfilment, refund and grant path would have to keep in sync
— which is precisely the class of bug Wave 1 had to fix elsewhere. A concurrently
changed *allowance* is a benign race (one grant lands a moment later); a
concurrently changed *usage* is the dangerous one, and that is read inside the
UPDATE.

**Precheck then authoritative recheck.** Upload paths run the cheap
`can_consume()` precheck early (so a doomed upload is rejected before a project
row exists), then the authoritative reservation inside the transaction. The
precheck is advisory; the UPDATE decides.

**Failed uploads never permanently reserve storage.** The reservation is part of
the request's transaction, so `db.session.rollback()` releases it — no separate
compensating write to get wrong.

**Proved by two tests, not by arguing about the math:**

1. *Stale-precheck test* — the overcommit scenario reproduced exactly: two
   consumers read the same headroom, both pass `can_consume`, the first
   reservation wins and the second is rejected **by the UPDATE**. Final usage 60,
   not 120.
2. *Threaded test* — 8 real threads on separate connections, released together
   by a barrier, each attempting a 25-byte reservation against a 100-byte
   allowance. Asserts exactly **4** winners (never 5) and a final value of
   exactly 100.

## 18. Focused test results

All green.

```
tests/integration/test_wave3_storage_accounting.py
tests/migrations/test_media_storage_ledger_migration.py
tests/integration/test_wave2_entitlement_foundation.py
    104 passed in 296.02s
```

| Area | Tests |
|---|---|
| Schema (BigInteger, validators, active-key dedup, excluded artifacts) | 4 |
| Migration (single head, columns/BigInteger, ON DELETE SET NULL, partial unique) | 4 |
| Entitlement resolver (three sources, over-storage, unstated base, independence) | 4 |
| Create/upload (within, exactly at, above, per-file independence, multi-pair whole-set) | 5 |
| Replacement (8-case policy matrix + 3 HTTP flows) | 11 |
| Deletion (success frees, failure does not, retry idempotence, archive frees nothing) | 4 |
| Transfer (sufficient, insufficient/no partial movement) | 2 |
| Add-on + admin grant (catalog-driven quantity, stacking across a plan change, refund overage, grant/revoke) | 4 |
| Reconciliation (dry run, apply, rerun, missing, orphan, ambiguous, size drift, never deletes) | 8 |
| Concurrency (stale precheck, 8 threads, failed upload) | 3 |

(38 test functions + 4 migration tests; the replacement policy matrix is
parametrized over 8 cases, giving 49 collected items.)

### Regression check on the touched areas

Focused, not the full suite — only the files covering code this wave changed.

```
tests/gate_jr/test_marker_selection_upload.py
tests/integration/test_resumable_upload.py
tests/integration/test_upload_edge_hardening.py
    111 passed, 1 skipped in 351.06s     (skip is the pre-existing Playwright test)

tests/integration/test_wave2_entitlement_foundation.py
tests/integration/test_addon_entitlements.py
tests/integration/test_admin_refunds.py
tests/integration/test_wave1_p0_blockers.py
tests/integration/test_v1_agent2_admin_parity.py
    176 passed, 3 failed -> 3 assertions updated, then all green
```

### The three updated assertions

Each was an explicit "Wave 3 owns this" placeholder, and Wave 3 is the change
they were waiting for. All three were updated to the new contract rather than
weakened:

| Test | Was | Now |
|---|---|---|
| `test_base_storage_entitlement_appears_in_resolver_but_is_not_metered` (Wave 2) | `storage_usage_tracked is False` | `is True`, plus real `storage_used_bytes` / `storage_remaining_bytes` / `over_storage` assertions |
| `test_user_profile_entitlement_summary_uses_backend_ledgers` (Agent 2) | asserted the "Storage usage is not measured yet" disclaimer renders | asserts it does **not** render, since it is now obsolete |
| `test_p0_2_invalid_addon_type_is_still_rejected` (Wave 1) | used `ACCOUNT_STORAGE` as a stand-in for an unsupported type | uses a genuinely invalid type, preserving what the test means to check |

No test was deleted, skipped or loosened.

## 19. Tests deliberately NOT run, and why

Per this checkpoint's explicit policy:

* **The full suite** (`python -m pytest -q`) — explicitly forbidden this
  checkpoint. Not run.
* **Full production / PostgreSQL certification lane** — the project lead runs
  final integration, full regression and PostgreSQL certification once, after
  this wave merges. Not run.
* **PostgreSQL `scanstory_qa`** — no reachable QA database was found under the
  prior waves' conventions, and credentials were never guessed or
  brute-forced. Focused migration/FK verification therefore ran on SQLite via
  the repo's established `tests/migrations/` harness. The PostgreSQL-specific
  constructs used (`ON DELETE SET NULL`, partial unique index, `BIGINT`,
  `ALTER COLUMN … TYPE BIGINT`) are all standard and are exercised by the
  chain's existing dialect-aware migration style; **this is the one item most
  worth confirming in the lead's certification run.**

## 20. Known limitations / deferred items

Explicit, not hidden.

1. **Existing media is not counted until an operator runs
   `flask reconcile-storage --apply`.** This is by design (§5), but it means a
   freshly migrated production database reports every account at 0 bytes used
   until that command is run. It should be part of the Wave 3 deploy runbook.
2. **No storage UI.** `storage_usage_tracked` is now True, so Agent 2's "not
   tracked yet" disclaimer stops rendering, but no meter replaces it yet. The
   resolver and `_entitlement_summary()` expose every number needed; rendering
   is a later checkpoint's job per this brief. *Worth sequencing soon* — the
   interim state shows an allowance with no usage beside it.
3. **No customer-facing storage-add-on purchase UI.** `ACCOUNT_STORAGE` is fully
   purchasable through the existing checkout machinery, and admins can now
   create/edit it at `/admin/addons` (the `storage_bytes_delta` field and the
   type option are wired) or seed it via `flask seed-addon-catalog`. What is
   deferred is the customer-facing add-on page listing it for self-service
   purchase.
4. **Admin grant route has no UI.** `POST /admin/users/<id>/grant-storage`
   exists and is permission-guarded; no button calls it yet.
5. **Ownership-transfer HTTP routes still do not exist**, so the transfer
   primitives are exercised through the service functions and tests, not through
   a live request. Explicitly out of scope per the brief.
6. **`processing_queue` / background compression does not adjust the ledger.**
   If video compression ever rewrites the *stored* video in place (today it
   writes a separate `_fast.mp4` derivative, which is deliberately uncounted),
   the ledger would need a hook. Reconciliation would report the drift and
   correct it; no live code path currently does this.
7. **`ProjectPair.image_size` / `video_size` remain `Integer`.** They are display
   metadata, not accounting — the ledger's `size_bytes` is BigInteger and is the
   billed number. Widening them is cosmetic and was left out of scope to keep
   the migration minimal.
8. **Per-account granularity only.** No per-project storage quotas, no storage
   analytics/history, no "top consumers" admin view. Not requested.
9. **Orphan files are reported, never adopted or removed.** Deliberate:
   automatically billing or deleting a file with no owner is exactly the kind of
   guess this wave refuses to make.
10. **The threaded concurrency test uses SQLite**, which serializes writers.
    It genuinely proves the conditional UPDATE never overcommits, but
    PostgreSQL's row-lock behaviour under real parallelism is the lead's
    certification run to confirm.

## 21. Merge risk

**Low–moderate.** Additive by design.

*Low risk:*

* New table, new module, new migration, new CLI, new tests — all additive.
* Only one template changed (`admin/addons.html`, three additive form/summary
  lines); no scanner file changed, no V1 branch or tag touched.
* Purchased/granted storage is composed at read time, so no re-materialization
  path can drop it and no existing plan-activation code needed changing.
* The migration is a strict superset: NULL `base_storage_bytes` means unenforced,
  so **no existing account gains a quota it did not have**.

*Where to look in review:*

* `user_edit_project` was restructured from a single interleaved loop into
  validate-then-commit phases. Behaviourally equivalent for the non-storage
  cases, and it fixes a latent crash (the old code did
  `os.path.join(IMAGES_DIR, pair.image_filename)` with `image_filename=None` for
  direct-QR pairs), but it is the largest single-function change in the diff.
* `_ResumableQuotaLimitReached` gained a code/message; the finalize handler now
  reports `limit_exc.code` instead of a hard-coded `PROJECT_LIMIT_REACHED`.
  Existing behaviour for the project-limit case is unchanged.
* `EntitlementTransaction.delta_value` widening is a real `ALTER COLUMN TYPE` on
  PostgreSQL (table rewrite on a small table).
* `storage_usage_tracked` flipping to True removes three template blocks from
  the rendered output. Intended; no test asserted their presence.

*Conflict surface with concurrent agents:* `app.py` (large file, several
regions), `models.py` (three regions), `entitlements.py` (one region),
`templates/admin/addons.html` (three lines). No overlap with scanner, user
templates or processing code.

## 22. Git status

Commits on `agent/v1.1-platform-admin` (post-sync base `2216183`) are listed in
§2. Five logical, checkpoint-scoped commits: schema+migration, resolver+service
module, app wiring, tests, docs.

At handoff `git status --short` is empty (all work committed) and
`git diff --check` is clean. `git diff --stat` against the post-sync base:

```
 app.py                                          | ~660 ++++++++++++++++++---
 entitlements.py                                 |   44 +++-
 models.py                                       |  109 +++++-
 storage_accounting.py                           |  new
 migrations/versions/f2b7d4e9c3a6_*.py           |  new
 templates/admin/addons.html                     |    4 +
 tests/integration/test_wave3_storage_accounting.py    |  new
 tests/migrations/test_media_storage_ledger_migration.py | new
 tests/integration/test_wave2_entitlement_foundation.py  |  ~7 (assertion update)
 tests/integration/test_wave1_p0_blockers.py             |  ~3 (assertion update)
 tests/integration/test_v1_agent2_admin_parity.py        |  ~3 (assertion update)
```

No
`.env`, `instance/`, `data/`, `data_admin/`, `routes_map.txt`, `server-tree.txt`,
`windows_rq_worker.py`, scratch file, test database, credential or secret was
staged, committed, printed or logged at any point.

## 23. Scanner algorithms — explicit untouched confirmation

Verified by `git diff --stat` against the post-sync base
`22161837f3b43c0abee542ae19f78b0148770adf`, which returned **empty** for:

* `scanner_runtime.py`
* `media_processing.py`
* `static/js/scanner-runtime.js`
* `templates/user/scanner.html`
* `compatibility_resolver.py`

All five are **byte-identical** to the starting commit.

No ORB, homography, RANSAC, optical-flow, calibration, tracking-geometry,
threshold or smoothing parameter was read into this work as anything other than
context. `media_processing.py` was read to understand the real storage path
scheme before designing `storage_key`; its processing logic was not modified.
No scanner, recognition or playback behaviour is altered by this wave.
