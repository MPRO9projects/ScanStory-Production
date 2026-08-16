# ScanStory V1.1 — Wave 2 (Agent 1): Commercial Entitlement Foundation

Branch: `agent/v1.1-platform-admin`
Worktree: `F:\ScanStory-main\ScanStory-v1.1-agent1`

Wave 2 builds the commercial entitlement **foundation** — schema, one central
resolver, and validation wiring — that Wave 3 storage accounting, plan UX,
upgrade/downgrade and add-on work will consume. It deliberately does **not**
implement storage-byte usage accounting.

---

## 1. Starting commit

`29b8db440a34bc139bd606fd692aa92388b93081`

Reached by syncing `develop/scanstory-v1.1` into `agent/v1.1-platform-admin`.
The merge was a **fast-forward** from `ad20da9063a74af0fbf3b39820923b6fad70704e`
(the Wave 1 tip) — **no conflicts**.

Pre-merge verification performed in this run:

| Check | Result |
|---|---|
| `git status --short` | clean |
| `git branch --show-current` | `agent/v1.1-platform-admin` |
| `git rev-parse HEAD` | `ad20da9063a74af0fbf3b39820923b6fad70704e` |
| `git rev-parse develop/scanstory-v1.1` | `29b8db440a34bc139bd606fd692aa92388b93081` |
| Merge | fast-forward, 23 files, no conflicts |

Wave 1 content confirmed genuinely present (not merely assumed):

* Commit `b2fb71f` "fix(prod): close the nine V1.1 P0 production blockers" is in
  history, touching `app.py`, `models.py`, `rate_limit.py`, `processing_queue.py`
  and adding `tests/integration/test_wave1_p0_blockers.py` (1130 lines).
* Both Wave 1 migrations exist in `migrations/versions/`:
  `c3f7a1d5e9b4_addon_catalog_type_check_allows_coverage.py` and
  `d4e8b2c6a0f3_upload_session_fk_set_null_on_delete.py`.
* Alembic chain was single-headed at `d4e8b2c6a0f3` before this wave.
* `tests/integration/test_wave1_p0_blockers.py` — **72 passed** on the synced
  tree before any Wave 2 behaviour change was made.

**Baseline-verification anomaly (disclosed).** My first two attempts to
re-verify the full `1576 passed / 4 skipped / 0 failed` baseline were started
while a previous run was still executing. The two runs overlapped on the same
SQLite test databases and temp upload directories and produced
`42 failed / 421 errors` and `41 failed / 599 errors` respectively. Those numbers
are **artefacts of concurrent execution, not real failures** — every suite run
serially afterwards was green. All subsequent runs in this wave were strictly
one-at-a-time. See §15 for the authoritative serial result.

---

## 2. Ending commit

See §19 for the exact commit id(s) recorded at hand-off.

---

## 3. Files changed

| File | Type | What |
|---|---|---|
| `entitlements.py` | **new** | Central effective-entitlement resolver + immutable server safety ceilings |
| `models.py` | modified | `SubscriptionPlan` policy fields, `User` pending-plan fields, `PaymentOrder` policy snapshot |
| `app.py` | modified | Resolver wiring, validity chaining, downgrade lifecycle, admin-grant normalisation, single entitlement-materialisation helper |
| `migrations/versions/e7a3f9c2b1d5_plan_commercial_policy_foundation.py` | **new** | Wave 2 migration |
| `tests/integration/test_wave2_entitlement_foundation.py` | **new** | 55 Wave 2 behaviour tests |
| `tests/migrations/test_plan_commercial_policy_migration.py` | **new** | 8 migration tests |
| `tests/migrations/test_migrated_schema_lane.py` | modified | Head assertion → ancestry assertion (see §17) |
| `tests/security/test_upload_validation.py` | modified | Ceiling monkeypatch retargeted to the canonical module (see §17) |

No scanner algorithm file was modified — see §20.

---

## 4. Migrations added and new Alembic head

**New revision:** `e7a3f9c2b1d5` — *plan commercial policy foundation*
**down_revision:** `d4e8b2c6a0f3` (the Wave 1 head)
**New head:** `e7a3f9c2b1d5`, chain remains **single-headed and linear**.

No historical revision was edited. Wave 1's `c3f7a1d5e9b4` and `d4e8b2c6a0f3`
are byte-identical and remain ancestors (asserted by test).

Migration properties:

* **Deterministic backfill, no invented commercial values.** Every default was
  chosen to preserve the *exact* current behaviour of every existing plan row:
  * `plan_family` → `INDIVIDUAL`. No pre-existing per-plan signal exists to
    infer a family from (no plan column has ever referenced `account_type`), and
    INDIVIDUAL is the only product that has shipped — a safe inference, not a
    guess at a commercial value.
  * `lifecycle_status` → `ACTIVE` (nothing currently sellable stops selling).
  * `plan_revision` → `1`.
  * All per-file media policy columns → `NULL`, meaning *"this plan imposes no
    cap of its own"*. Because enforcement is `min(plan, server ceiling)`, NULL
    leaves the server ceiling as the only effective limit — byte-for-byte the
    pre-Wave-2 rule.
  * `base_storage_bytes` → `NULL` ("unspecified"). No storage entitlement has
    ever been sold; inventing a quota here would *create* a commercial term.
  * `allow_direct_qr` / `allow_detect_once` / `allow_tracked_overlay` → `TRUE`,
    because every plan can currently create every combination. Defaulting these
    off would retroactively revoke capability from live accounts.
* **BigInteger** for every byte-valued column (`max_image_bytes`,
  `max_video_bytes`, `max_image_pixels`, `base_storage_bytes`). Integer caps at
  ~2.1 GB, the risk the Wave 1 audit flagged; asserted by a test that reflects
  the column type.
* **PostgreSQL compatible**: `server_default` supplied for every NOT NULL column
  so existing rows backfill in a single ALTER; booleans use `sa.true()`/
  `sa.false()` (dialect-correct, not a bare `1` which PostgreSQL rejects for a
  boolean column); adds wrapped in `batch_alter_table` for SQLite's table-rebuild
  semantics; the new FK is explicitly named so `downgrade()` can drop it on
  PostgreSQL, where an unnamed FK gets a server-assigned name.
* **Downgrade safe**: drops exactly what it added; upgrade→downgrade→upgrade
  round-trip is tested.
* Column adds are guarded by an inspector check, so a partially-applied state
  does not hard-fail.

---

## 5. Exact SubscriptionPlan fields added/changed

**Changed:** nothing. Every pre-existing column (`total_project_limit`,
`total_scan_limit`, `max_pairs_per_project`, `duration_type`, `duration_value`,
`plan_amount`, `offer_price`, `currency`, `is_active`, …) is untouched and
still authoritative. Wave 2 extends *around* them.

**Added to `subscription_plans`:**

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| `plan_family` | String(30) | NOT NULL | `INDIVIDUAL` | `INDIVIDUAL` \| `BUSINESS_VENDOR`; indexed |
| `lifecycle_status` | String(40) | NOT NULL | `ACTIVE` | `DRAFT` \| `ACTIVE` \| `CLOSED_FOR_NEW_PURCHASE` \| `ARCHIVED`; indexed |
| `plan_revision` | Integer | NOT NULL | `1` | Commercial-policy version marker |
| `max_image_bytes` | **BigInteger** | NULL | — | Per-file image size policy |
| `max_video_bytes` | **BigInteger** | NULL | — | Per-file video size policy |
| `max_video_duration_seconds` | Integer | NULL | — | Per-file video duration policy |
| `max_image_dimension_px` | Integer | NULL | — | Per-file image dimension policy |
| `max_image_pixels` | **BigInteger** | NULL | — | Per-file image pixel-count policy |
| `base_storage_bytes` | **BigInteger** | NULL | — | Base **account storage entitlement** (allowance only) |
| `allow_direct_qr` | Boolean | NOT NULL | `TRUE` | May create/change into Direct QR |
| `allow_detect_once` | Boolean | NOT NULL | `TRUE` | May create/change into Detect Once |
| `allow_tracked_overlay` | Boolean | NOT NULL | `TRUE` | May create/change into Tracked Overlay |

The five media-policy fields deliberately mirror the arguments
`upload_validation.validate_image(file, tmp, max_bytes, max_dimension_px,
max_pixels)` and `validate_video(file, tmp, max_bytes, max_duration_seconds)`
*actually take*, so there is no plan field that enforcement cannot consume.

**Added to `users`** (downgrade lifecycle):

| Column | Type | Null | Purpose |
|---|---|---|---|
| `pending_plan_id` | Integer FK → `subscription_plans.id` | NULL | Parked (deferred) plan change |
| `pending_plan_effective_at` | DateTime | NULL | The term boundary it applies at |

**Added to `payment_orders`** (lifecycle/versioning foundation):

| Column | Type | Null | Purpose |
|---|---|---|---|
| `plan_policy_snapshot_json` | Text | NULL | Full commercial policy the subscriber agreed to |
| `is_deferred_plan_change` | Boolean | NOT NULL (`FALSE`) | Marks a downgrade purchase |

**Model-level additions:** `plan_family` and `lifecycle_status` validators
(normalise to upper-case, reject unknown values), `SubscriptionPlan.is_purchasable`,
`SubscriptionPlan.policy_snapshot()`, `PaymentOrder.plan_policy_snapshot`.

**Plan lifecycle / versioning — what was built and what was NOT.**
The smallest sound thing the current architecture supports was implemented:
a **status enum** governing purchasability, a **revision marker**, and a
**purchase-time snapshot** persisted on `PaymentOrder`. `PaymentOrder` already
captured a partial snapshot (`purchased_project_limit` / `purchased_scan_limit`),
so this extends an existing pattern rather than inventing one. Residual risk is
stated honestly in §17 — this is *not* a full immutable-revision billing system.

**Relationship note.** `User` now has two FKs to `subscription_plans`, so
`SubscriptionPlan.users` required an explicit `foreign_keys="User.subscription_id"`;
a second relationship `pending_users` covers `User.pending_plan_id`. Without this
SQLAlchemy cannot disambiguate the join and every model import fails.

---

## 6. Entitlement resolver contract

**Where:** `entitlements.py` — one module, one entry point.

```python
get_effective_entitlements(user, unlimited_override=False) -> dict
```

`app.user_entitlements(user)` is the thin app-side wrapper that supplies
`unlimited_override=has_dev_test_entitlement(user)`, so no call site has to
remember the dev-test rule. `unlimited_override` is a parameter rather than an
import because `entitlements.py` must not import `app.py` (circular).

Returned keys:

*Plan identity / lifecycle* — `plan_id`, `plan_name`, `plan_family`,
`plan_revision`, `plan_lifecycle_status`, `plan_is_purchasable`.

*Project capacity* — `base_project_limit`, `purchased_project_capacity`,
`admin_granted_project_capacity`, `effective_project_limit`, `projects_used`,
`projects_remaining`, `over_project_capacity`.

*Scans* — `base_scan_limit`, `purchased_scan_capacity`,
`admin_granted_scan_capacity`, `effective_scan_limit`, `scans_used`,
`scans_remaining`.

*Pairs* — `max_pairs_per_project` (**server ceiling already applied**).

*Storage (entitlement only)* — `base_storage_bytes`, `purchased_storage_bytes`
(always `0` this wave), `effective_storage_bytes`, `storage_usage_tracked`
(always `False`).

*Experience* — `allow_direct_qr`, `allow_detect_once`, `allow_tracked_overlay`,
`allowed_playback_modes` (a set).

*Per-file media policy, hard cap already applied* —
`image_policy = {max_bytes, max_dimension_px, max_pixels}`,
`video_policy = {max_bytes, max_duration_seconds}`.

*Account / term state* — `subscription_status`, `subscription_expires_at`,
`has_active_subscription`, `pending_plan_id`, `pending_plan_effective_at`,
`unlimited`.

Helpers: `image_limits(e)` and `video_limits(e)` return tuples in exactly the
argument order `validate_image` / `validate_video` take; `cap(plan, ceiling)`
implements the min() rule; `allowed_playback_modes(plan)`;
`plan_pairs_limit(plan)`; `ledger_breakdown(user, type)`; `is_downgrade(a, b)`.

**Source-of-truth discipline (unchanged from Wave 1).** The materialised columns
`User.subscribed_project_limit` / `subscribed_scan_limit` remain the
**enforcement** values, because `_reserve_project_quota_atomic` and
`_consume_scan_quota_atomic` compare against them inside a single atomic
conditional UPDATE. Computing limits dynamically would mean replacing that with
a read-then-write and losing the concurrency guarantee. The resolver's plan +
ledger numbers are the **audit view** of how those columns were composed. There
is exactly one addition of ledger to plan, so no double counting.

**Refactor scope.** Deliberately narrow. Only the sites that actually needed the
new logic were changed: `get_plan_pairs_limit`, the four
`validate_image`/`validate_video` call sites, the resumable-upload declared-size
precheck, `_resolve_project_experience_playback`, and the two admin scan routes.
No sweeping rewrite of unrelated routes.

**Coverage vs entitlement stay separate.** The resolver returns nothing named
`coverage` (asserted by a test). Project service coverage — *"is this project's
public availability still paid for"* — remains entirely in
`ProjectServiceCoverage` / `apply_standalone_project_renewal`. Entitlement is
*"what is this account currently allowed to do"*. The two are never conflated.

---

## 7. Upgrade validity chaining implementation

Implemented in `activate_subscription_from_order` (`app.py`) — the deferred
Wave 1 decision, now made.

```python
has_paid_term_remaining = (
    user.subscription_status == "active"
    and user.subscription_expires_at
    and user.subscription_expires_at > now
)
subscription_end = _add_calendar_months(now, plan.duration_value or 0)
if has_paid_term_remaining and not defer_change:
    subscription_end += (user.subscription_expires_at - now)
```

* **Effective immediately** after confirmed payment (plan id, limits and status
  are applied in the same transaction).
* **Unused paid validity is preserved/chained**, not discarded: the new term is
  *appended* to whatever remains of the paid term.
* **Only paid time chains.** The guard is `subscription_status == "active"`,
  which excludes `trial` — a trial has no paid validity to preserve. Tested.
* **Idempotent replay preserved.** The chaining is computed *before* the
  existing conditional `UPDATE … WHERE status = 'pending'`. On a replayed
  activation that UPDATE matches 0 rows, the function returns early with
  `replay: True`, and the user row is never touched — so validity cannot be
  chained twice. This reuses Wave 1's idempotency guard rather than adding a
  second, racier mechanism. Tested explicitly.
* **Purchased add-ons survive** and **usage counters are not reset** — both
  Wave 1 properties, re-asserted by Wave 2 tests.
* `_add_calendar_months` (real calendar months, Wave 1's P0-1/ANM-41 fix) is
  unchanged.

`user = User.query.get(...)` moved above the order UPDATE because the chained
end-date must be known before it is written into the order. It is a read only.

---

## 8. Downgrade lifecycle implementation

**Detection** — `entitlements.is_downgrade(current_plan, new_plan)`. True if any
of `total_project_limit`, `total_scan_limit`, `max_pairs_per_project` strictly
decreases (with `None`/`0` ranked as *unlimited*, i.e. infinitely high, so
unlimited → finite **is** a downgrade), or **any** experience entitlement is
lost (set difference, not strict subset — swapping one premium mode for another
still removes something), or a stated `base_storage_bytes` decreases (compared
only when both sides state a number, since `NULL` means "unspecified", not
"unlimited").

**Deferral** — in `activate_subscription_from_order`:

```python
defer_change = has_paid_term_remaining and is_downgrade(current_plan, plan)
```

When deferring, the purchase is fully recorded (order → `success`, policy
snapshot written, `is_deferred_plan_change = True`, reservation activated) but
the user's plan, limits and expiry are **left exactly as they are**. Only
`pending_plan_id` and `pending_plan_effective_at = subscription_expires_at` are
set. The call returns `{"success": True, "deferred": True, "effective_at": …}`.

A downgrade with **no paid term left to wait for** is just an ordinary
activation and applies immediately — there is no boundary to defer to.

**Application at the term boundary** — `apply_pending_plan_change_if_due(user)`,
called from the top of `check_user_limits(user)`.

No new cron or job system was invented. `check_user_limits` already runs on every
gated user action and already handles subscription expiry (it is the code path
that flips `active` → `expired`), so it **is** this codebase's existing
term-boundary hook. The function is strictly additive: it sets the new plan,
recomputes the materialised columns *through the ledger* (so purchased and
admin-granted entitlement survive the downgrade), clears the pending fields, and
**deletes nothing**.

An immediate (upgrade / like-for-like) activation clears any parked downgrade,
because the term it was scheduled against no longer exists in that form. Tested.

**Documented gap:** a user who never returns is not transitioned until their next
gated request. This is harmless here — an unapplied downgrade only ever leaves
the account on a *higher* allowance, never a lower one, and no billing is driven
off this field. Marked in-code with a `ponytail:` comment naming the upgrade
path (a scheduled sweep) if a background job ever needs the downgraded state
without a user request.

No proration or refund engine was built.

---

## 9. Grandfathering behaviour

All three required behaviours are implemented and tested.

**Pairs.** `_reserve_pair_slots_for_project` is the single choke point.
* An existing project over the pair limit **remains valid** — nothing is deleted.
* Adding another pair **while over** is blocked (`existing + requested > max`).
* Deleting/reducing is unaffected.
* **Replacement is count-neutral**: `user_edit_project` replaces media on
  existing pairs and never calls the pair gate, so it proceeds provided media
  policy passes. One fix was required here: the gate previously rejected a
  *zero-growth* request on an already-over-limit project. Since this gate is
  about **growth**, `requested_pairs == 0` now short-circuits to allowed. This
  is the direct expression of the locked rule "replacing an existing pair does
  not increase pair count and may proceed".
* **Lowering a plan's pair limit never deletes pairs** — tested.

**Playback mode.** `_enforce_experience_entitlement` is only ever reached from
`_resolve_project_experience_playback`, which is only called on **create /
change-into** paths. An existing project is therefore never re-checked:
* A grandfathered premium mode **continues working** after a downgrade removes
  the entitlement — tested by flipping `allow_detect_once` off and asserting the
  stored `playback_mode` is unchanged and still served.
* Replacing media does **not** force a mode downgrade (the edit path does not
  touch `playback_mode` at all).
* **Changing into** another non-entitled premium mode **is blocked** — tested.
* Invalid experience/playback *combinations* are still rejected as invalid
  **before** the entitlement check runs, so entitlement can never turn an
  invalid pairing into a valid one — tested.

**Media.** Existing oversized/long media is untouched — nothing scans or
re-validates stored files. **Replacement must satisfy the CURRENT policy**:
`user_edit_project` now resolves `image_limits`/`video_limits` from the
resolver and passes them to the validators.

---

## 10. Admin grant behaviour

**Audit finding.** Two pre-existing admin routes silently destroyed purchased
entitlement — the same class of bug as Wave 1's P0-1:

* `admin_update_scan_limit` did `user.subscribed_scan_limit = new_scan_limit`,
  overwriting the materialised column and **deleting purchased EXTRA_SCANS**.
* `admin_grant_extra_scans` did a bare `user.subscribed_scan_limit += extra`
  with **no ledger row**, so the grant was **erased by the next plan
  activation** (which rebuilds the column from plan + ledger).

**Normalisation.** Admin grants now reuse the existing `EntitlementTransaction`
ledger — no new table was invented — with a new `source_type` of `admin_grant`:

* `admin_grant_extra_scans` routes through `_apply_entitlement_transaction(...)`,
  keyed on the `AdminActivity` row id, which makes it **auditable** (it links to
  the admin action that caused it), **non-destructive**, **idempotent** on
  `(source_type, source_id, entitlement_type)`, and **survivable** across plan
  activation. `log_admin_activity` now returns the created row so its id is
  available. Compatible with future storage/admin grants (same ledger, new type).
* `admin_update_scan_limit` now treats the admin-entered number as the **base**
  allowance and re-adds the ledger on top.

**Distinguishability.** `ledger_breakdown()` splits each dimension into
`purchased` (all non-admin sources) and `admin_granted`, surfaced separately as
`purchased_scan_capacity` / `admin_granted_scan_capacity` (and the project
equivalents). Plan entitlement, purchased add-ons, and admin grants are
therefore three distinguishable quantities and **none can silently overwrite
another**. `total` remains what the materialised column reconciles against, so
the split is reporting and can never drift from enforcement.

No arbitrary grant values were invented — this is structure only.

**One writer.** Wave 1 shipped a structural test asserting that exactly one
function assigns the materialised entitlement columns. Wave 2 added two more
legitimate writers, so rather than weaken that guard, a single
`materialize_plan_entitlements(user, plan_project_limit, plan_scan_limit)`
helper was extracted and **all three** call sites (activation, deferred plan
change, admin scan-limit edit) now route through it. The Wave 1 guard passes
unmodified.

---

## 11. Hard server ceiling behaviour

The immutable ceilings are **existing application constants**, not new
inventions. They were moved from `app.py` into `entitlements.py` so the resolver
and the upload paths share **one** definition instead of two that can drift:

`MAX_IMAGE_SIZE` (50 MB), `MAX_VIDEO_SIZE` (1 GB), `MAX_IMAGE_DIMENSION_PX`
(8000), `MAX_IMAGE_PIXELS` (40 M), `MAX_VIDEO_DURATION_SECONDS` (unset ⇒ no
check), `MAX_PAIRS_PER_PROJECT_CEILING` (10). All remain env-overridable
deployment configuration.

**Rule:** effective limit = `cap(plan_policy, ceiling)` = `min()` of the stated
values; `None` on either side means that side imposes no limit; both `None`
means no check at all (preserving the pre-Wave-2 duration behaviour).

* A plan **below** the ceiling is the effective limit.
* A plan **above** the ceiling is silently capped **down** to the ceiling — an
  admin can never raise a plan past a safety ceiling. Tested for bytes,
  dimensions, pixels and pairs.
* Ceilings are **not exposed as editable Admin fields**.

**`MAX_CONTENT_LENGTH` compatibility.** `ABSOLUTE_MAX_REQUEST_BYTES` is still
derived from the *ceilings* (`(MAX_VIDEO_SIZE + MAX_IMAGE_SIZE) × pairs ceiling
+ overhead`), not from plan policy. Since plan policy can only ever *lower* the
effective per-file limit, the largest legitimate request shape is unchanged and
the Wave 1 cap remains correct and sufficient. The Wave 1 test asserting
`cap >= MAX_VIDEO_SIZE + MAX_IMAGE_SIZE` still passes.

**Canonical-module discipline (important for future work).** `app.py` still
exposes the same constant names, but they are **import-time snapshots** used only
to size the request cap and for value comparisons in tests. Every *enforcement*
site reads `_ent.MAX_*` or the resolver at call time. `entitlements.py` is the
single canonical definition and therefore the only correct patch point — this is
now stated in-code and two security tests were retargeted accordingly (§17).

---

## 12. Deferred Wave 3 storage work

Explicitly **not** built, as instructed:

* No `MediaObject` model, no authoritative storage ledger.
* No `storage_bytes_used` accounting or counters.
* No filesystem scanning for billing usage.
* No delete/replacement byte reconciliation.
* No transfer storage accounting.
* No "account storage +GB" add-on (plumbing only — the add-on itself is not
  implemented, and `+pairs` / per-file-size / duration / image-dimension /
  playback-mode add-ons remain **plan differentiators**, never purchasable).

What Wave 2 *does* provide for Wave 3: `SubscriptionPlan.base_storage_bytes`
(BigInteger, the allowance), and a resolver shape that already exposes
`base_storage_bytes`, `purchased_storage_bytes` (hard `0`) and
`effective_storage_bytes`, plus `storage_usage_tracked = False` so no consumer
can mistake an allowance for metered usage. Wave 3 can sum a purchased-storage
ledger into `purchased_storage_bytes` and flip `storage_usage_tracked` **without
a breaking shape change** — asserted by a contract test.

---

## 13. Focused test results

All serial, `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest`.

| Suite | Result |
|---|---|
| `tests/integration/test_wave1_p0_blockers.py` (post-sync, pre-change) | **72 passed** |
| `tests/integration/test_wave1_p0_blockers.py` (post-change) | **72 passed** |
| `tests/integration/test_wave2_entitlement_foundation.py` | **55 passed** |
| `tests/migrations/test_plan_commercial_policy_migration.py` | **8 passed** |
| `tests/migrations` (whole lane) | **85 passed, 3 skipped** |
| `tests/security/test_upload_validation.py` | **28 passed** |
| `tests/unit tests/models tests/contracts tests/compatibility tests/security tests/ops` | **182 passed** |

Wave 2 coverage maps to the brief:

* **Plan family** — individual, business/vendor, case normalisation, invalid
  family rejected, family surfaced in the resolver, all seeded plans have one.
* **Lifecycle** — default ACTIVE/purchasable, CLOSED_FOR_NEW_PURCHASE not
  purchasable, invalid status rejected, purchase-time snapshot immune to later
  plan edits.
* **Experience entitlement** — each of Direct QR / Detect Once / Tracked Overlay
  allowed and blocked; invalid experience↔playback mapping still rejected
  regardless of plan; grandfathered mode continues after downgrade; changing
  into a blocked premium mode rejected.
* **Per-file limits** — plan below ceiling honoured; plan above ceiling capped
  by `min()`; NULL policy preserves pre-Wave-2 behaviour; `cap()` semantics;
  limit tuples match validator signatures; 64-bit byte columns.
* **Pairs** — under-limit add succeeds; at-limit add blocked; over-limit
  grandfathered project keeps its pairs and cannot grow; replacement is
  count-neutral; ceiling caps a greedy plan.
* **Entitlement composition** — base capacity; purchased capacity stacks;
  purchased scans survive activation; admin grants distinguishable from
  purchased; admin grant does not erase purchased; admin setting a limit
  preserves purchased; storage field and plan family in the resolver;
  resolver-shape stability for Wave 3.
* **Upgrade** — effective immediately; unused paid validity chained; trial
  validity not chained; idempotent replay does not chain twice; counters not
  reset; add-ons survive.
* **Downgrade** — detection (incl. experience loss and unlimited→finite); not
  applied mid-term; applied at the boundary; not applied before it; projects,
  pairs, premium mode and purchased entitlement all preserved; new actions
  respect the lower policy once effective; upgrade clears a parked downgrade.
* **Migrations** — single linear head; Wave 1 revisions untouched ancestors;
  upgrade from Wave 1 head; BigInteger columns; behaviour-preserving backfill of
  a real pre-Wave-2 row; indexes; downgrade; round-trip.
* **Separation** — resolver exposes no coverage concept.

---

## 14. Real PostgreSQL test results

**Not achieved — honest miss, with the reason.**

The QA PostgreSQL lane is driven by `SCANSTORY_QA_DATABASE_URL`
(`tests/migrations/test_migrated_schema_lane.py`), which skips rather than
passing vacuously when unset.

What was actually established this run:

* `SCANSTORY_QA_DATABASE_URL` is **not set** in this environment, and there is
  **no `.env`** in this worktree (only `.env.example`).
* The only documented local convention is `.env.example` line 62:
  `postgresql://qa:qa@localhost:5432/scanstory_qa`.
* A PostgreSQL server **is running and reachable** on `localhost:5432` — the
  attempt returns `FATAL: password authentication failed for user "qa"`, which
  is an *authentication* failure, not connection-refused. So the server (and
  very likely the `scanstory_qa` database) exists, but the documented credentials
  do not work.
* The correct driver is `psycopg` (v3, `postgresql+psycopg://`); `psycopg2` is
  not installed — `requirements.txt` pins `psycopg[binary]<=3.2.3`. A naive
  `postgresql://` URL fails with `ModuleNotFoundError: psycopg2`, which is worth
  recording for whoever runs this lane next.

Per instruction I did **not** guess or brute-force credentials, and no
credential value is printed anywhere in this report.

**Mitigation.** The migration was written for PostgreSQL correctness by
construction and the risky spots were handled deliberately, not by luck:
`server_default` on every NOT NULL column; `sa.true()`/`sa.false()` rather than a
bare `1` (which PostgreSQL rejects for a boolean column); an explicitly named
foreign key so `downgrade()` can drop it where the server would otherwise assign
its own name; BigInteger for byte columns. The 3 skipped migration tests are
exactly this lane.

**To close this gap:** set `SCANSTORY_QA_DATABASE_URL` to a disposable
PostgreSQL database using the `postgresql+psycopg://` scheme and re-run
`python -m pytest tests/migrations`. The 3 skips become real assertions.

---

## 15. Full regression result

See §19 for the final recorded numbers from the single authoritative serial run
(`python -m pytest -q` from the worktree root).

Note the §1 caveat: the only non-green totals observed in this wave came from two
*concurrently executing* runs sharing test databases, and were not reproducible
serially.

---

## 16. Skipped tests and why

* **3 skipped in `tests/migrations`** — the real-PostgreSQL migrated-schema lane,
  guarded by `requires_postgres` on `SCANSTORY_QA_DATABASE_URL`. Skipped because
  no working credentials were discoverable (§14). This lane is deliberately
  designed to skip loudly rather than pass vacuously.
* The remaining skips are pre-existing environment-gated tests inherited from the
  Wave 1 baseline (4 skipped there), not introduced by Wave 2.

---

## 17. Warnings / anomalies

1. **Two pre-existing tests were modified.** Both were mechanically invalidated
   by legitimate Wave 2 work, and both were handled following precedent already
   set in this repository rather than by weakening intent:
   * `tests/migrations/test_migrated_schema_lane.py::test_wave1_revisions_keep_a_single_linear_head`
     asserted `get_current_head() == d4e8b2c6a0f3`. Adding any new revision
     necessarily breaks that. Converted to a single-head + **ancestry** assertion
     — exactly the treatment Wave 1 itself applied to
     `test_admin_refunds_migration.py` when its revision stopped being head. The
     `down_revision` assertions for both Wave 1 revisions are retained.
   * `tests/security/test_upload_validation.py::test_oversize_{image,video}_rejected`
     monkeypatched `app_module.MAX_*`. Enforcement now reads the canonical
     `entitlements` module, so the patch was retargeted there. **This was a real
     finding, not test churn**: it exposed that my first cut left two names for
     one ceiling with enforcement reading only one. Fixed at the root by routing
     every enforcement site through `_ent.MAX_*` / the resolver at call time. The
     tests now additionally prove the `min(plan, ceiling)` path works end-to-end
     through a live upload request.
2. **Residual risk — plan versioning is a marker + snapshot, not immutable
   revisions.** An admin editing a live plan still mutates the `subscription_plans`
   row. What is protected is the **historical record**: every activation from now
   on persists a full `plan_policy_snapshot_json` on its `PaymentOrder`, so what a
   subscriber agreed to is recoverable even after the plan moves on, and
   `lifecycle_status` lets a plan be retired from new purchase without deleting
   it. **Not solved:** `plan_revision` is not auto-incremented on edit (the admin
   plan-edit route does not bump it), and a *currently active* subscriber's
   in-flight entitlements still read from the live plan row rather than from
   their snapshot. Orders placed **before** Wave 2 have `NULL` snapshots — which
   means "no snapshot captured", never "no policy applied". Closing this properly
   means resolving live entitlements from the subscriber's snapshot, which is a
   materially larger change than this wave's remit and would touch every
   entitlement read path. Documented rather than half-built.
3. **Deferred-downgrade application is request-triggered**, not scheduled (§8).
   Fail-safe direction (user stays on the *higher* allowance until they return).
4. **No experience-mode *change* route exists today.** The requirement "changing
   into a non-entitled premium mode is blocked" is satisfied at the create path,
   and `/projects/<id>/edit` only replaces media — it cannot alter
   `experience_type` or `playback_mode` at all. The gate lives in the shared
   `_resolve_project_experience_playback` choke point, so any future
   change-mode route inherits enforcement automatically. Stated so nobody
   assumes a change-route was tested.
5. **`log_admin_activity` now returns the created row** (previously returned
   `None`). Purely additive; no existing caller inspects the return value.
6. Pre-existing `LegacyAPIWarning` noise from SQLAlchemy `Query.get()` throughout
   the suite — inherited, not introduced, not addressed.

---

## 18. Merge risk

**Low–moderate.**

*Low risk:* `entitlements.py` and both new test files are new files — no merge
surface. The migration is a new file on a single linear chain; if another agent
also adds a revision, the only conflict is a `down_revision` re-point, which is
mechanical.

*Moderate risk — the real hotspots:*

* **`models.py` `SubscriptionPlan`** gained 12 columns and
  `SubscriptionPlan.users` gained a mandatory `foreign_keys=` argument. Any agent
  touching plan columns or that relationship will conflict. The `foreign_keys=`
  is **not optional** — without it every model import raises an ambiguous-join
  error, so it must survive any conflict resolution.
* **`app.py` `activate_subscription_from_order`** was restructured (user loaded
  earlier, downgrade branch added). Agent 2's admin/payment UI work is the most
  likely collision.
* **`app.py` `check_user_limits`** gained one call at the top.
* **The Wave 1 one-writer guard** (`test_p0_1_browser_and_webhook_share_one_activation_path`)
  will fail on any merge that reintroduces a direct
  `user.subscribed_*_limit = reconciled_*(...)` assignment. This is the guard
  working as designed — resolve by routing through
  `materialize_plan_entitlements()`.
* Agent 2 rewrote many `templates/admin/*.html` in the merged commit. Wave 2
  added **no** admin template changes, so there is no overlap there — but the new
  plan fields are consequently **not yet exposed in the Admin plan form**
  (`add_plan.html` / `edit_plan.html`). Plans can only be given non-default
  policy programmatically for now. That is a deliberate scope boundary, not an
  oversight.

---

## 19. git status

Recorded at hand-off — see the final response for the exact values.

Intended change set (nothing else):

```
 M app.py
 M models.py
 M tests/migrations/test_migrated_schema_lane.py
 M tests/security/test_upload_validation.py
?? entitlements.py
?? migrations/versions/e7a3f9c2b1d5_plan_commercial_policy_foundation.py
?? tests/integration/test_wave2_entitlement_foundation.py
?? tests/migrations/test_plan_commercial_policy_migration.py
```

`git diff --check` — **clean**.

No `.env`, `instance/`, `data/`, `data_admin/`, `routes_map.txt`,
`server-tree.txt`, `windows_rq_worker.py`, scratch file, test database,
credential or secret is staged or committed. No V1 branch or tag
(`release/scanstory-v1-server`, `hardening/saas-v1-production`, `v1.0.0-rc1`,
`v1.0.0-rc2`) was checked out, modified, or referenced. `ScanStory-integration`
was never touched.

---

## 20. Scanner algorithm files were NOT modified

Verified with `git diff --quiet <starting-commit> -- <path>` against the
post-sync starting commit `29b8db44`:

| File | Result |
|---|---|
| `scanner_runtime.py` | **UNCHANGED** |
| `media_processing.py` | **UNCHANGED** |
| `compatibility_resolver.py` | **UNCHANGED** |
| `static/js/scanner-runtime.js` | **UNCHANGED** |
| `templates/user/scanner.html` | **UNCHANGED** |

No ORB descriptor, homography, RANSAC, optical-flow, camera-calibration,
geometry, threshold or smoothing code was read for modification or altered in
any way. Tracked Overlay is never referred to as "Object Tracking" anywhere in
the code, tests, or this report.
