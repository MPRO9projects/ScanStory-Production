# ScanStory V1.1 Wave 1 P0 Implementation Report

Implementation checkpoint closing exactly the nine P0 blockers from Section 3 of
`SCANSTORY_V1_1_END_TO_END_PRODUCTION_AUDIT.md`. No V1.1 commercial/storage
architecture was started. No scanner algorithm was touched.

## Baseline

| Item | Value |
| --- | --- |
| Worktree | `F:\ScanStory-main\ScanStory-v1.1-agent1` |
| Branch | `agent/v1.1-platform-admin` |
| Starting commit | `f9cec78c2cdc35744e325c106d4b0a94f9569889` |
| Sync required | None. `develop/scanstory-v1.1` was already at the same commit; nothing to merge, no conflicts. |
| Working tree at start | Clean apart from the untracked audit file, as expected. `git diff --check` clean. |
| Python | `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe` (authoritative venv only) |
| Alembic head at start | `b2c4d6e8f0a1` — single head, 15 linear revisions |
| Regression baseline inherited from the audit | 1 failed, 1487 passed, 1 skipped (the failure is P0-9) |

Every one of the nine findings was re-verified against the current code before
any change. **All nine were CONFIRMED**; line numbers had shifted from those
quoted in the audit but every defect was present exactly as described.

---

## P0-1 Result

**CONFIRMED.** `activate_payment()` (now `app.py:8606`, audit cited 8632-8640)
contained all three defects verbatim: `subscribed_scan_limit` raw-overwritten
from `plan.total_scan_limit`, `projects_used = 0`, `scans_used = 0`, and
`timedelta(days=plan.duration_value * 30)`.

### Implementation

* Added `purchased_scan_capacity(user)` and `reconciled_scan_limit(user, base)`
  as an exact mirror of the existing, working `purchased_project_capacity()` /
  `reconciled_project_limit()` pair — the smallest safe reconciler the brief
  asked for, not a general entitlement resolver. `None`/`0` keep meaning
  "unlimited", matching the project convention.
* `user.subscribed_scan_limit = reconciled_scan_limit(user, plan.total_scan_limit)`,
  so purchased `EXTRA_SCANS` survive every activation and renewal.
* Removed both counter resets. `projects_used` / `scans_used` are the
  materialised columns that `_reserve_project_quota_atomic` and
  `_consume_scan_quota_atomic` gate against inside a single conditional UPDATE;
  they already track reality and are decremented on project delete, so the
  correct action on activation is to leave them alone. Zeroing them was handing
  a user a full fresh allowance on top of the projects they already owned.
* `PROJECT_CAPACITY` preservation via `reconciled_project_limit()` was verified
  intact and is now covered by its own regression test so the scan change cannot
  break it.
* Month arithmetic fixed: added `_add_calendar_months()` (stdlib `calendar`,
  clamping 31 Jan + 1 month to 28/29 Feb) replacing `duration_value * 30`. A
  plan advertising "1 Year" via `duration_display` now grants a real year rather
  than 360 days. This is unambiguous — the advertised term and the granted term
  simply disagreed — so it was fixed rather than deferred.

### Deferred to P1 / commercial follow-up

**Validity chaining on early upgrade is DEFERRED, explicitly and deliberately.**
`subscription_end = now + duration` still discards unused remaining time when a
user upgrades mid-term. Chaining it requires a commercial policy decision this
checkpoint is not authorised to make: whether unused days on a cheaper plan
carry onto a more expensive one at face value, pro-rata, or not at all. The
`VALIDITY_EXTENSION` add-on chains correctly because it extends the *same*
entitlement; a plan change does not. Flagged for the commercial wave, not
silently skipped.

### Tests

`tests/integration/test_wave1_p0_blockers.py`, P0-1 group (11 tests): activation
with no add-ons; EXTRA_SCANS survives; PROJECT_CAPACITY still survives; existing
project usage unchanged; existing scan usage unchanged; same-plan renewal does
not reset; duplicate browser verification idempotent; webhook replay idempotent;
structural assertion that `/verify-payment`, the webhook and the reconcile CLI
all route through the one `activate_payment()` and that exactly one site assigns
each materialised column; reconciler unit behaviour; calendar-month arithmetic.

Two pre-existing characterization tests asserted the buggy reset and were
updated with an explanatory comment
(`test_payment_idempotency_and_capacity.py`, `test_quota_characterization.py`).

---

## P0-2 Result

**CONFIRMED.** `migrations/versions/f4a8c2b91d70:34` still carried
`addon_type IN ('EXTRA_SCANS', 'VALIDITY_EXTENSION', 'PROJECT_CAPACITY')` with
no later revision amending it, while `models.ADDON_TYPES` and
`app.ADDON_PURCHASABLE_TYPES` both include `PROJECT_SERVICE_COVERAGE`.
Constraint name confirmed as `ck_addon_catalog_type`.

### Implementation

* **New revision `c3f7a1d5e9b4`** (`down_revision = b2c4d6e8f0a1`). The
  historical revision was not edited. It drops and recreates the CHECK inside
  `batch_alter_table` (SQLite-compatible, consistent with every other constraint
  change in this chain), tolerating a missing constraint on databases built by
  `create_all()`. The new predicate is a strict superset of the old one, so
  existing rows are preserved by construction; genuinely invalid types are still
  rejected. `downgrade()` is implemented and **refuses** rather than silently
  destroying data if any row already uses the newly permitted type.
* **Root-cause fix for the drift itself**: `AddonCatalog` gained a matching
  `__table_args__` CheckConstraint. Its absence is precisely why the suite —
  which builds schema with `db.create_all()` — could never see the constraint.
  Model and migration are now in lockstep.
* `ACCOUNT_STORAGE` was **not** added, per the wave boundary.

### Tests

Wave 1 file: coverage row can be created; invalid type still rejected; model
declares the same constraint; historical revision unedited. Migrated-schema lane
(below): previous-head→new-head upgrade permits the type; fresh-DB→head; invalid
type rejected against the *migrated* schema; existing rows survive the
replacement; downgrade restores the narrower constraint.

---

## P0-3 Result

**CONFIRMED.** Zero `AddonCatalog(` constructor calls outside `models.py`; no
seed, no migration insert, no Admin route. `GET /api/addons/catalog` returns
`[]` on any freshly migrated database.

`VALIDITY_EXTENSION` was checked for deadness as instructed: it is **still
live** — handled by `_addon_effect()`, present in `ADDON_PURCHASABLE_TYPES`, and
handled by the refund-reversal path. It was kept as a supported type.

### Implementation

* **New permission `superadmin.addons.manage`**, superadmin-only, added to
  `ADMIN_ROLE_PERMISSIONS` and to `HIGH_IMPACT_PERMISSIONS` so denials are
  audit-logged by the existing decorator.
* **Admin surface** (`templates/admin/addons.html` + four routes):
  `GET /admin/addons`, `POST /admin/addons/create`,
  `POST /admin/addons/<id>/edit`, `POST /admin/addons/<id>/toggle`. Every
  mutating action writes an `AdminActivity` row through the existing
  `log_admin_activity` pattern. CSRF tokens on every form, consistent with the
  rest of the admin surface.
* **No delete route exists at all.** Toggling `is_active` /
  `is_commercially_available` is the only removal mechanism, because
  `AddonPurchase` and the entitlement ledger reference catalog rows by id and
  the refund-reversal path re-reads them. A hard delete would orphan purchase
  history and break refunds.
* Form validation reuses `_addon_effect()` — the exact function the purchase
  path uses — so an item that would be rejected at checkout cannot be saved as
  available.
* **Bootstrap**: `seed_addon_catalog_items(entries)` performs an idempotent
  upsert keyed on `code`, exposed as `flask seed-addon-catalog`. Source is
  `--file`, `ADDON_CATALOG_SEED_FILE` or `ADDON_CATALOG_SEED_JSON`. It is
  **dry-run by default**. With no configured source it prints an explanation and
  exits non-zero rather than inventing prices — **no commercial values are
  hard-coded anywhere in this change**.
* Refund and fulfilment compatibility preserved: no existing add-on code path
  was modified.
* `ACCOUNT_STORAGE` not added.

### Tests

11 tests: superadmin create + edit; actions audit-logged; plain admin denied on
both read and write; no delete route in the URL map; referenced item survives
deactivation with its purchase intact; inactive and unlisted items excluded from
the commercial API while the live one is returned; seeding idempotent across two
runs; `PROJECT_SERVICE_COVERAGE` seedable (depends on P0-2); CLI refuses to
invent prices; CLI dry-run then apply.

---

## P0-4 Result

**CONFIRMED.** `_delete_project_files_and_rows()` hard-coded `IMAGES_DIR` /
`VIDEOS_DIR` / `FEATURES_DIR` / `QR_DIR` with two bare `except Exception: pass`
loops and no logging.

### Implementation

* New `project_media_dirs(project)` resolves the directory set from **actual
  project ownership** (`owner_admin_id` → `data_admin/*`), mirroring the
  existing `processing_operations._dirs_for_project` convention. The calling
  route no longer influences which directories are touched.
* New `_safe_media_path(directory, filename)` basenames the stored filename and
  verifies the resolved path's parent is exactly the media root, returning
  `None` otherwise. Stored names are server-generated but are persisted and
  never re-validated on read, so the delete path no longer trusts them.
  Traversal cannot escape the root.
* New `_unlink_project_media()` treats a missing file as success (idempotent,
  re-runnable) but **surfaces real failures**: each is logged as
  `project_media_unlink_failed` with the basename only and returned to the
  caller; an aggregate `project_delete_incomplete_media_cleanup` is logged if any
  remain. Nothing is swallowed. The helper now returns the failure list.
* Derived artifacts the old helper never removed are now cleaned up:
  `_work.jpg` intermediates and `_fast.mp4` compressed variants.
* No account storage accounting was introduced (Wave 2).
* User-facing responses are unchanged and carry no path or exception text.

### Tests

8 tests: user project removes exactly its files; **admin project removes its
files, with the fixture asserting it really wrote into `data_admin/`** (the
actual fix under test); directory resolution follows ownership not caller;
unrelated project files untouched; missing files safe; unlink failure surfaced
and logged (both log lines asserted); delete response leaks no absolute path or
traceback; traversal cannot escape the media root.

---

## P0-5 Result

**CONFIRMED.** `UploadSession.project_id` / `pair_id` referenced
`projects.id` / `project_pairs.id` with no `ondelete` and were never cleared.

### Retention decision

UploadSession rows are **operational/audit history** — they carry
`failure_code`, byte offsets, checksums and timings, and they are the data
behind the Admin upload-diagnostics panel. They are therefore **retained** with
their references nulled, not cascade-deleted. Both columns were already
nullable, which is what makes `SET NULL` the correct and smallest design.

### Implementation

* **New revision `d4e8b2c6a0f3`** (`down_revision = c3f7a1d5e9b4`). It reflects
  the existing server-assigned constraint names (the originals were created
  unnamed), drops them and recreates both foreign keys with
  `ondelete="SET NULL"` inside `batch_alter_table`. `downgrade()` recreates them
  without the clause.
* Model declaration updated to match.
* Enforced at the **database** level rather than only in the delete helper,
  deliberately: the ORM cascades from `Admin.projects` and `User` bypass the
  helper entirely, and a fix that only lived in the helper would leave those
  paths broken. The helper additionally nulls the references explicitly so
  behaviour is identical on a SQLite database running without
  `PRAGMA foreign_keys=ON`.

### Tests

Wave 1 file: delete a resumably-uploaded project → no error, session row
survives with both references `NULL`; schema-level assertion that both FKs
declare `SET NULL`. Migrated-schema lane: FK reflection after `upgrade head`;
delete-with-session against the migrated schema with foreign keys **actually
enforced**; upgrade and downgrade paths. PostgreSQL-only: a test that drops the
cascade and proves the original `IntegrityError` occurs — i.e. proof the lane
would have caught this — is present and skipped without a QA database.

---

## P0-6 Result

**CONFIRMED.** Mode resolution still fell through to `fake`;
`queue_available()` returned `True` unconditionally for `fake`/`inline`;
`_readiness_checks()` only probed Redis when `mode == "rq"`; and
`SCANSTORY_TESTING` was absent from the production prohibition list.

### Implementation

* `SCANSTORY_TESTING=1` added to the production prohibition beside
  `SCANSTORY_DEV_TESTING`. On a production host it permitted SQLite and forced
  `fake` — total silent degradation.
* **Explicit environment contract.** Production was detected only by an opt-in
  flag, so a deploy setting none of `SCANSTORY_PRODUCTION` / `APP_ENV` / `ENV` /
  `FLASK_ENV` booted happily into the dead-pipeline state. A non-testing runtime
  that declares no environment now **refuses to boot** with an actionable
  message. This is non-bypassable by omission, which was the entire failure
  mode. Any explicit value satisfies it, so development is unaffected.
* `queue_available()` now returns `False` for `fake`/`inline` when
  `queue_required()` — fail closed.
* `_readiness_checks()`: a production runtime in any non-`rq` mode is now a
  not-ready condition rather than a reason to skip the check. In non-production
  runtimes the resolved mode is reported in the payload, so a degraded mode is
  visible instead of indistinguishable from a real queue.
* `/healthz` deliberately unchanged — still a static, dependency-free liveness
  probe.
* No SMTP or Razorpay reachability was added to readiness (out of scope, and it
  would make `/ready` fragile). No worker-heartbeat system was built; worker
  liveness stays P1/Admin-monitoring scope as the brief permits.

### Tests

10 tests: production + `fake` → startup failure; production + `inline` →
startup failure; production + `SCANSTORY_TESTING=1` → startup failure;
production + missing Redis → startup failure; undeclared environment → startup
failure; `queue_available()` fails closed in production and stays permissive in
development; `/ready` 503 when production runs a fake queue; `/ready` 503 when
Redis is down in rq mode; `/ready` 200 for a valid rq configuration; `/healthz`
stays lightweight.

Two pre-existing readiness assertions were updated for the new payload key, with
comments.

---

## P0-7 Result

**CONFIRMED.** `MAX_CONTENT_LENGTH` was unset by default, so absolute ingest was
unbounded at every layer this repository controls.

All multipart routes were audited first: `POST /upload` (`handle_upload`),
`POST /projects/<id>/edit` (`user_edit_project`),
`POST /admin/projects/upload` (`admin_handle_upload`),
`POST /detect_init`, `POST /detect_track` (small per-frame test images), and the
resumable chunk route (already bounded by `RESUMABLE_UPLOAD_CHUNK_MAX_BYTES`
with its own 413).

### Implementation — a two-tier application + proxy contract

1. **Absolute ceiling, always applied.** `MAX_CONTENT_LENGTH` is now always set.
   When the env var is unset it is *derived from the hard limits already in the
   codebase*:
   `((MAX_VIDEO_SIZE + MAX_IMAGE_SIZE) * MAX_PAIRS_PER_PROJECT_CEILING) + 8 MiB`
   — 11,270,094,848 bytes with shipped defaults. Finite, and large enough that no
   legitimate multi-pair upload is broken. Werkzeug rejects on `Content-Length`
   before the body is read.
2. **Small default per-request cap.** A `before_request` hook applies
   `MAX_REQUEST_BODY_BYTES` (default 64 MiB) to every endpoint that is *not* one
   of the three multi-pair upload routes, rejecting with a 413 JSON body on
   `Content-Length` alone — before any parsing, decode or disk spooling.

A naive small global cap was explicitly avoided; the resumable path is
untouched and its 1 MiB chunk cap sits comfortably below the default tier, so
the two never contradict each other.

### Server-team contract (documented, not invented)

`docs/production/README.md` gained a **Reverse-Proxy Ingest Contract** section
stating the required relationships (upload locations
`client_max_body_size >= MAX_CONTENT_LENGTH`; chunk location
`>= SCANSTORY_RESUMABLE_CHUNK_MAX_BYTES` and not greatly above; server default
`<= MAX_REQUEST_BODY_BYTES`; timeouts sized for the slowest supported upload),
explicitly marked `SERVER-TEAM-VERIFY` and tied to questionnaire Q17/Q18/Q59.
**No Hostinger or Nginx values were invented.** `.env.example` documents all
three new variables.

### Tests

7 tests: absolute cap configured and finite; body above the ceiling rejected
with 413; non-upload endpoint rejected at the smaller cap with
`REQUEST_TOO_LARGE`; the three multi-pair endpoints keep the large allowance
while others do not; oversized body rejected with `validate_image`/
`validate_video` monkeypatched to explode, proving nothing is parsed first;
normal request unaffected; resumable chunk cap still bounded and below the
default tier.

---

## P0-8 Result

**CONFIRMED.** Neither `/admin/login` nor `/admin/forgot-password` called
`_check_rate_limit` anywhere, and `RATE_LIMIT_REDIS_URL` was read by no code.

A second process-local limiter would have reproduced the same class of bug, so
the existing `rate_limit.py` / `_check_rate_limit` / `_rate_limit_key`
infrastructure was **extended and centralized** rather than duplicated.

### Implementation

* `rate_limit.py` now offers one API — `check(key, limit, window) ->
  (allowed, retry_after)` — with two interchangeable backends:
  `InMemoryRateLimiter` (unchanged behaviour; deterministic, used by
  development and the test suite) and a new `RedisRateLimiter` (fixed window via
  `INCR` + `TTL` + `EXPIRE`, namespaced, shared across workers and across
  restarts). `build_limiter()` selects on `RATE_LIMIT_REDIS_URL`. A malformed
  URL raises loudly rather than silently downgrading to an ineffective limiter.
* **Redis-unavailable policy: FAIL CLOSED**, documented in the module docstring
  and in `docs/production/README.md`. Rationale: the endpoints behind this
  limiter are authentication, OTP mail and abuse-reporting paths where an
  unlimited window is a real security exposure; a Redis outage already takes
  `/ready` to 503 because RQ needs the same Redis, so the deployment is out of
  rotation regardless; allowing unmetered credential spray during that window
  would trade a bounded availability problem for an unbounded security one.
  Denials return a short `Retry-After` (5s) so recovery is immediate.
* New `identity_digest()` — identifiers are SHA-256 hashed before entering a
  key, since keys reach Redis and logs. **No OTP, password, token or signature
  is ever passed into a key or a log line.**
* Limits added and wired:

  | Endpoint | Buckets |
  | --- | --- |
  | `POST /admin/login` | `20 / 900s` per IP **and** `10 / 900s` per identity+IP |
  | `POST /admin/forgot-password` | `10 / 3600s` per IP **and** `3 / 3600s` per identity+IP |
  | `POST /login/` | existing `80 / 900s` per IP **plus new** `15 / 900s` per identity+IP |

  Two buckets per auth route deliberately: the IP bucket stops one host spraying
  many identities, and the tighter identity+IP bucket carries the per-account
  limit *without* a single abusive client being able to deny an entire NAT'd
  network or lock a known admin out from elsewhere — exactly the combination the
  brief specified.
* Admin login is limited **before** any DB lookup or password hashing. Admin
  forgot-password is limited **before** `_create_otp` is called, closing the
  unlimited mail-bomb trigger that bypassed the `_resend_otp` throttles.
* `Retry-After` headers preserved/added on all limited HTML responses.
* Existing limiters (register, forgot-password, resend-OTP, upload, content
  report, all scanner routes) required **no call-site change** — they already
  route through `_check_rate_limit`, so they inherit the Redis backend
  automatically. The content-report limiter's process-local problem is fixed by
  that inheritance, not by a new mechanism.
* `POST /webhooks/razorpay` deliberately **not** rate limited; a test asserts
  this stays true.

### Tests

15 tests: two "worker-like" limiter instances sharing one fake Redis observe a
single counter (a process-local limiter would have allowed double); counter
survives a simulated process restart; namespaces independent; Redis-unavailable
fails closed with the documented `Retry-After`; testing fallback is in-memory;
misconfigured URL raises; identity digest never echoes the identifier and is
stable; admin login IP bucket triggers 429 with `Retry-After`; admin login
identity bucket triggers; admin forgot-password triggers; **OTP creation stops
once limited** (mail-bombing blocked before an OTP is minted); user login has no
behaviour regression; webhook not limited (structural); exactly one
`from rate_limit import` in `app.py` (no second parallel mechanism).

---

## P0-9 Result

**CONFIRMED.** `project_public_access_state()` (`app.py:2099`) resolved the
owner only via `project_current_owner_user_id()`, which returns
`current_owner_user_id or owner_user_id` — both NULL for an admin-owned project.

### Implementation — product semantics, not a test patch

Added an `owner_admin_id` branch that establishes an `ADMIN_OWNED` coverage
source when the project has **no user owner** and the owning `Admin` row is
**active**. Design constraints, all held:

* It is a **real authorization fact** (an active platform admin owns this row),
  not a synthesized fake paid `User` subscription.
* `project.is_active` is still checked first, so admin suspension and moderation
  continue to work unchanged.
* The branch is gated on there being no user owner, so a project transferred to
  a user is judged purely by the user rule — the admin branch cannot become a
  permanent bypass.
* The user-owned invariant `Project.is_active AND valid coverage` is untouched;
  no inactive or uncovered user project becomes public as a side effect.
* Private-cache semantics on the admin media routes are unchanged.

This is the fix for the one currently-failing regression test,
`tests/security/test_security_health_performance.py::test_admin_media_uses_private_cache_not_public`.

### Tests

7 tests covering **image, video and QR**: admin-owned project resolves
`ADMIN_OWNED` coverage; all three media routes serve; private-cache header
present and `public` absent; suspended admin project still gated (via
`is_active`, the right mechanism); project of a deactivated admin is not
covered; user-owned expired project remains unavailable; admin-created project
transferred to an expired user follows the user rule.

---

## PostgreSQL Verification

**Honest status: the PostgreSQL lane was BUILT and is RUNNABLE, but was NOT
EXECUTED against PostgreSQL in this checkpoint, because usable credentials could
not be obtained without guessing.**

What was checked:

* Something is listening on `localhost:5432` — confirmed.
* `psycopg` 3.2.3 is installed in the authoritative venv (`psycopg2` is not).
* No `.env` exists in this worktree; the only env file anywhere nearby is
  `../ScanStory-main/.env.test.example`. A repo-wide search found **no reference
  to `scanstory_qa`** or to any QA PostgreSQL DSN in `scripts/`, `tests/`,
  `docs/`, `.env.example`, `pytest.ini` or `run-tests.ps1`.
* Two standard local-dev conventions were attempted once each (trust/no-password
  and `postgres`/`postgres`). Both were refused by the server. **No further
  attempts were made and nothing was brute-forced.** No credential was printed.

What was built instead — `tests/migrations/test_migrated_schema_lane.py`, an
**additive** lane that does not replace the fast SQLite suite:

* It runs `alembic upgrade head` rather than `db.create_all()`, so it tests the
  **schema that actually ships** — the systemic gap behind both P0-2 and P0-5.
* Engine is parameterized: set `SCANSTORY_QA_DATABASE_URL` to a **disposable**
  PostgreSQL database and the whole lane runs there, dropping and recreating the
  `public` schema around each test. With it unset the lane still runs, on a
  temporary SQLite file with **`PRAGMA foreign_keys=ON`** so foreign keys are
  genuinely enforced rather than silently ignored.
* Three PostgreSQL-only tests are marked `skipif` with an explicit reason rather
  than passing vacuously: FK enforcement without the cascade (proving the lane
  would have caught P0-5), `SELECT ... FOR UPDATE` on the pair-quota path, and
  `pg_constraint` name reflection.
* The docstring and `.env.example` both warn never to point it at production.

### Verification status by claim

| Claim | Verified on |
| --- | --- |
| Migrations reach head from empty; single linear head | Migrated schema (SQLite lane) |
| `PROJECT_SERVICE_COVERAGE` insertable after upgrade | Migrated schema (SQLite lane) |
| Invalid addon type still rejected | Migrated schema (SQLite lane) |
| Existing rows survive constraint replacement | Migrated schema (SQLite lane) |
| Constraint downgrade restores narrower predicate | Migrated schema (SQLite lane) |
| `UploadSession` FKs declare `SET NULL` | Migrated schema (SQLite lane) |
| Project delete with an upload session succeeds, references nulled | Migrated schema, **FKs enforced** (SQLite lane) |
| PostgreSQL FK enforcement / `FOR UPDATE` / `pg_constraint` | **NOT VERIFIED — skipped, no credentials** |
| All P0 behavioural fixes | SQLite (`create_all`) fast suite |

**Required follow-up before rc1:** provision a disposable `scanstory_qa`
PostgreSQL database, set `SCANSTORY_QA_DATABASE_URL`, and re-run
`tests/migrations/test_migrated_schema_lane.py`. Three tests will unskip. This
is a credentials task, not an engineering one — the lane is complete.

---

## New Migrations

| Revision | Down revision | Purpose |
| --- | --- | --- |
| `c3f7a1d5e9b4` | `b2c4d6e8f0a1` | Replace `ck_addon_catalog_type` so `PROJECT_SERVICE_COVERAGE` is permitted (P0-2) |
| `d4e8b2c6a0f3` | `c3f7a1d5e9b4` | `upload_sessions.project_id` / `pair_id` → `ON DELETE SET NULL` (P0-5) |

* **New head: `d4e8b2c6a0f3`.** Single head, still strictly linear, now 17
  revisions.
* No historical migration was edited — asserted by a test.
* Both have real `downgrade()` implementations. `c3f7a1d5e9b4`'s downgrade
  refuses rather than destroying rows that use the newly permitted type.
* Both use `batch_alter_table` for SQLite compatibility, consistent with the
  chain.
* `ACCOUNT_STORAGE` was not added, no plan columns were added, no storage
  ledger was created.
* `tests/migrations/test_admin_refunds_migration.py` asserted
  `b2c4d6e8f0a1` was the head; rewritten to assert single-headedness and chain
  membership instead, which is the durable invariant.

---

## Full Regression

Command (authoritative venv, per Section 32 of the audit):

```
F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest -q
```

**Result: `1570 passed, 4 skipped, 4696 warnings in 2279.88s (37m59s)`. ZERO FAILURES.**

| | Audit baseline | This checkpoint |
| --- | --- | --- |
| Failed | **1** (`test_admin_media_uses_private_cache_not_public`, P0-9) | **0** |
| Passed | 1487 | 1570 |
| Skipped | 1 (Playwright crop test) | 4 (Playwright crop test + 3 PostgreSQL-only lane tests) |
| Warnings | 4591 | 4696 |

The exit condition is met: the red baseline is now fully green. Net +83 tests
(83 new Wave 1 and migrated-schema-lane tests, minus none removed). The three
additional skips are the new PostgreSQL-only lane tests, each with an explicit
skip reason — they are not silent passes.

Warning count rose by 105, entirely `LegacyAPIWarning` from `Query.get()` in the
new tests, which follow the surrounding suite's existing style. **No new warning
class was introduced.**

### Grouped gates run before the full regression

| Gate | Result |
| --- | --- |
| Wave 1 P0 regression file + all migration tests | 149 passed, 3 skipped |
| Security, payments, add-ons, refunds, uploads, queue/readiness, authz, runtime hardening | 324 passed, then 67 passed on the four re-run files after the readiness-payload and environment-declaration test updates |

`git diff --check` clean.

---

## Remaining P1

Untouched this wave and still open, in the audit's own numbering: P1-01/02
(raw `str(e)` leakage), P1-03/04/05 (logging level, discarded `extra=`
telemetry, `print()` channel), P1-06 (no error monitoring), P1-07 (worker
liveness, media writability, migration-head in `/ready`), P1-08 (login lockout
keyed on `user_id`, never cleared), P1-09 (`is_blocked` unchecked on the four
resumable routes), P1-10 (reCAPTCHA fails open), P1-11/12 (session lifetime,
no regeneration on login), P1-13/14 (phantom `permissions_json`, three
bare-`current_admin()` routes), P1-16/17 (undecorated admin media routes,
`serve_qr` unparsed-name gate), P1-18/19/20/21 (client limit desync, MOV
handling, non-atomic multi-pair edit, `LOAD_TRUNCATED_IMAGES` thread safety),
P1-22 (four maintenance CLIs unscheduled), P1-23/24 (no retry path, RQ
scheduler disabled), P1-25/26/27 (synchronous SMTP, `SMTP_SECURITY=none`,
contact-form header/HTML injection), P1-28 (`ProjectServiceCoverage.EXPIRED`
never written), P1-29/30/31 (refund manual-review queue, capacity reversal,
money-before-DB ordering), P1-32/33/34 (moderation transitions, hash salt,
cascade), P1-35 (user-delete cascade vs `PaymentRefund`), P1-36 (`run-tests.ps1`
hard-coded root and bare `python`), P1-37 (stale production docs), P1-38
(`bc5642a86981` duplicate preflight).

P1-15's rate-limit gap list was **partially** closed as a side effect of P0-8:
the central mechanism now exists and admin login, admin forgot-password and
user login identity buckets are wired. The nine endpoints P1-15 names
(`/create-razorpay-order`, `/verify-payment`, the two add-on routes, the four
`/api/uploads/sessions*` routes, `/send-contact-email`) were **not** wired, to
avoid changing unrelated product behaviour in a P0 checkpoint. They are now a
small task on top of an existing foundation rather than architecture.

### Tightly-coupled adjacent changes made, disclosed

Per the "do not invent a P0-10" rule, every change outside the nine blockers:

1. **Calendar-month arithmetic** (ANM-41) — inside the P0-1 function, four
   lines, unambiguous, and leaving it would have meant shipping a known
   wrong-by-5-days validity on the very line being fixed.
2. **Model-level `ck_addon_catalog_type`** — strictly necessary for P0-2 to be
   a root-cause fix rather than a one-off patch; without it the drift that hid
   the bug remains.
3. **`_work.jpg` / `_fast.mp4` cleanup** in the delete helper — one line in the
   loop being rewritten for P0-4, directly adjacent, prevents further orphans.
4. **`UploadSession` explicit detach** in the delete helper alongside the schema
   `ondelete` — belt-and-braces so behaviour is identical on SQLite without
   `PRAGMA foreign_keys=ON`.
5. **`login_identity` bucket on user login** — required by the brief's own P0-8
   instruction to combine identifier+IP.
6. **`/ready` reports the queue mode** in non-production — the visibility half
   of P0-6; a silently degraded mode was the defect.

Nothing else was changed. No P1 was promoted into this wave.

---

## Remaining Commercial Work

Explicitly **not started**, as instructed — all of it is Wave 2+:
`plan_family`; per-plan image/video byte limits, video duration, image pixel
limits; `base_storage_bytes`; the account storage ledger / `media_object` table
/ `User.storage_bytes_used`; `ACCOUNT_STORAGE` add-on type and
`AddonCatalog.storage_bytes_delta`; `BigInteger` widening of
`EntitlementTransaction.delta_value`; plan experience-entitlement flags; plan
lifecycle states and plan versioning; downgrade and grandfathering
architecture; pricing redesign; customer storage dashboard; storage-on-transfer
accounting; per-pair delete UX; the ownership transfer/claim HTTP surface;
`get_effective_entitlements(user)`; admin grants routed through the ledger; and
any `app.py` restructuring.

Two items this wave leaves as direct inputs to that work: the deferred validity
chaining decision (P0-1), and per-plan add-on pricing, which the new
`/admin/addons` surface and `seed-addon-catalog` command now make enterable
without a schema change.

---

## Server-Team Dependencies

| # | Item | Blocking |
| --- | --- | --- |
| Q17 | `client_max_body_size` globally and for `/upload`, `/projects/*/edit`, `/admin/projects/upload`, `/api/uploads/sessions/*/chunk` | P0-7. The app side is now bounded and documented; the proxy relationships in the new README section must be confirmed and evidenced. |
| Q18 | `proxy_read_timeout`, `proxy_send_timeout`, `proxy_connect_timeout`, `client_body_timeout` | P0-7. Must accommodate the slowest supported upload. |
| Q58 | Exact production environment set, specifically `SCANSTORY_PRODUCTION`/`APP_ENV`/`ENV`/`FLASK_ENV`, `SCANSTORY_QUEUE_MODE`, `SCANSTORY_TESTING`, `REDIS_URL` | P0-6. The app now refuses to boot undeclared, so the deployed unit **must** set one of these. This is a deploy-configuration change the server team has to make before the next release. |
| Q59 | `RATE_LIMIT_REDIS_URL` provisionable | P0-8. Without it the limiter stays process-local and every limit is multiplied by the worker count. Note the fail-closed policy. |
| Q11 | Gunicorn worker count | Interpreting P0-8 limits and DB pool sizing. |
| New | A disposable `scanstory_qa` PostgreSQL database and credentials | The PostgreSQL lane. Three tests unskip immediately. |

No Hostinger, Nginx or PostgreSQL values were invented anywhere in this
checkpoint.

---

## Git State

* Starting commit: `f9cec78c2cdc35744e325c106d4b0a94f9569889`
* Branch: `agent/v1.1-platform-admin` (worktree `ScanStory-v1.1-agent1` only)
* `ScanStory-integration` was never touched.
* No V1 branch or tag was touched: `release/scanstory-v1-server`,
  `hardening/saas-v1-production`, `v1.0.0-rc1`, `v1.0.0-rc2` are unmodified.
* No scanner algorithm was modified: ORB descriptor matching, homography,
  RANSAC, optical flow, tracking geometry, camera calibration, overlay
  perspective math, scanner thresholds, smoothing and target-guide algorithms
  are all byte-identical. The only scanner-adjacent change is the P0-9
  coverage/ownership fix, which is authorization logic, not scanner maths.
* Nothing excluded was committed: no `.env`, `instance/`, `data/`,
  `data_admin/`, `routes_map.txt`, `server-tree.txt`, `windows_rq_worker.py`,
  test DBs, scratch files or credentials.

### Commits

| Commit | Contents |
| --- | --- |
| `b2fb71f` | `fix(prod): close the nine V1.1 P0 production blockers` — all nine fixes, both migrations, the admin add-on surface, the Wave 1 regression tests and the migrated-schema lane, plus the config/doc updates |
| `12417d1` | `docs(v1.1): add Wave 1 P0 implementation report` — this document and the audit status note |

The split is deliberate and both commits are independently green. A four-way
split by blocker was considered and rejected: the fixes share `app.py`,
`activate_payment()`/`_delete_project_files_and_rows()` and a single regression
test file, and P0-3's tests depend on P0-2 while P0-5's schema revision chains
onto P0-2's — separating them would have produced broken intermediate commits,
which the checkpoint brief explicitly rules out. This also matches the
one-commit-per-checkpoint convention used by the preceding V1.1 commits on this
branch.

Working tree clean after both commits. `git diff --check` clean.
