# ScanStory V1.1 End-to-End Production Audit

> **IMPLEMENTATION STATUS NOTE (added after the audit; findings below are unchanged).**
> All nine Section 3 P0 blockers were re-verified against the code and closed in the Wave 1
> checkpoint. See `SCANSTORY_V1_1_WAVE1_P0_IMPLEMENTATION_REPORT.md` for what was implemented,
> what was deliberately deferred (validity chaining on early upgrade), and what remains
> `SERVER-TEAM-VERIFY`. Two new Alembic revisions were added (`c3f7a1d5e9b4`, `d4e8b2c6a0f3`);
> the head is no longer `b2c4d6e8f0a1`. Section 2's note that the full regression was not
> completed within the audit window is superseded by the result recorded later in Section 32.
> Nothing in Sections 3-38 below has been rewritten.

Audit-only. No application code, schema, Admin UI, scanner algorithm, or runtime behaviour was
changed in the production of this document. The only repository mutation performed was the
Phase 0 sync of `agent/v1.1-platform-admin` with `develop/scanstory-v1.1`, which resolved as a
clean fast-forward (no merge commit, no conflicts).

Classifications used throughout: `PASS`, `PARTIAL`, `MISSING`, `MISCONFIGURED`, `UNTESTED`,
`SERVER-TEAM-VERIFY`, `ADMIN-UI-CANDIDATE`, `SUPERADMIN-ONLY`, `SERVER-ONLY`, `DO-NOT-EXPOSE`,
`BLOCKER`, `P1-HARDENING`, `P2-POST-GO-LIVE`, `SCHEMA-CHANGE-REQUIRED`, `MIGRATION-REQUIRED`,
`BACKFILL-REQUIRED`, `TEST-GAP`.

---

## 1. Executive Verdict

**LOCAL CODE READINESS = CONDITIONAL PASS.** The V1 core — resumable upload protocol, Razorpay
signature/webhook idempotency, OTP lifecycle, CSRF coverage, ownership-checked routes, secret
handling, atomic project/scan/capacity reservation — is genuinely well engineered and in several
places better than typical for this stage. It is held below an unconditional pass by nine
concrete defects (Section 3), of which three are commercial-integrity bugs in the paid path, two
are PostgreSQL-only failures that the SQLite test suite structurally cannot catch, one is a
silent-degradation configuration hole in the job queue, two are unbounded-resource/abuse
exposures, and one is a functional regression that is **already failing in the full test suite**.

**V1.1 COMMERCIAL ENTITLEMENT READINESS = REQUIRES IMPLEMENTATION.** Of the fourteen locked plan
attributes, six exist (`price`, `billing term`, `project capacity`, `scan allowance`,
`max_pairs_per_project`, `availability` as a bare `is_active` boolean), one is partial (plan
lifecycle), and seven are absent from the schema entirely (`plan_family`, `max_image_bytes`,
`max_video_bytes`, `max_video_duration`, `max_image_pixels/dimensions`, `base_storage_bytes`,
experience-mode entitlement flags). Most consequentially, **account storage does not exist as a
concept anywhere in the product** — there is no column, no ledger, no rollup, and no query that
can produce a trustworthy per-account byte total today. The locked storage model, the reusable
storage add-on, storage-on-transfer, and every `OVER_STORAGE` rule are therefore greenfield, not
extensions. Grandfathering likewise has no representation.

**ADMIN OPERATIONS READINESS = PARTIAL.** The RBAC model itself (`ADMIN_ROLE_PERMISSIONS`,
`require_admin_permission`, last-superadmin protection) is sound and correctly applied to the
overwhelming majority of routes. The Admin plan editor exposes only 13 of the ~20 fields the
locked commercial model needs, edits live `SubscriptionPlan` rows in place with no versioning or
impact preview, and there is no Admin surface at all for storage, upload diagnostics, backup
status, migration head, worker liveness, or the refund `MANUAL_REVIEW_REQUIRED` queue. There is a
genuine operations page (`/admin/operations`) with SMTP and RQ diagnostics — a good foundation
that is materially incomplete against the locked requirement set.

**SERVER READINESS = UNVERIFIED.** This audit ran entirely on a local Windows worktree. The
repository contains **no** Gunicorn, Nginx, systemd, Docker, or Procfile configuration of any
kind, and `docs/production/` documents required proxy behaviour in prose only, without a
`client_max_body_size` value, without proxy timeouts, and without a worker count against which
the 30-connections-per-process SQLAlchemy pool could be sized. Nothing about the production host
can be certified from here. All 60 questionnaire groups in Section 33 are `SERVER-TEAM-VERIFY`.

**PRODUCTION CERTIFICATION READINESS = NOT READY.** Blocked on: the nine P0 items, the
commercial entitlement implementation (schema + backend + Admin UI + creator UI + tests), a
PostgreSQL-executed test pass (the entire suite runs on SQLite today, so the row-locking and
foreign-key paths are untested by construction), server verification, and load measurement.

---

## 2. Audited Baseline

| Item | Value |
| --- | --- |
| Worktree | `F:\ScanStory-main\ScanStory-v1.1-agent1` |
| Branch | `agent/v1.1-platform-admin` |
| Commit before sync | `b496192f0a249865a9dc3993359f8e8c5f1060fd` |
| **Audited commit (post-sync)** | **`f9cec78c2cdc35744e325c106d4b0a94f9569889`** |
| Sync result | Fast-forward from `b496192` to `f9cec78`. No merge commit, no conflicts, `git diff --check` clean, working tree clean before and after. |
| Latest merge on baseline | `f9cec78 Merge branch 'agent/v1.1-experience-ux' into develop/scanstory-v1.1` |
| Python | `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe` (authoritative venv) |
| Alembic head | `b2c4d6e8f0a1` (`admin payment refunds`) — single head, 15 revisions, strictly linear |
| Test collection | **1489 tests collected, 0 collection errors** |
| **Full regression (executed in this audit)** | **1 failed, 1487 passed, 1 skipped, 4591 warnings in 42m43s.** The failure is `tests/security/test_security_health_performance.py::test_admin_media_uses_private_cache_not_public` — **not flaky, a real defect on the baseline** (see P0-9). The skip is a Playwright-dependent crop test. The audit brief's "252 passed, 0 failed" refers to a focused subset, which does not include this test |
| Application DB (production) | PostgreSQL — enforced at startup (`app.py:123-128`); SQLite/non-Postgres URLs are rejected outside testing |
| Test DB | **SQLite only** (`tests/conftest.py:22-28,51`). No PostgreSQL test path exists; `run-tests.ps1:23-26` actively rejects a non-SQLite `DATABASE_URL` |
| Queue | Redis + RQ, queue `scanstory-processing`, `RQ_DEFAULT_TIMEOUT=600` (`processing_queue.py:56-62`) |
| App entrypoint size | `app.py` = 13,522 lines; `models.py` = 2,045 lines |

### Local-environment limitations (bounding every claim in this document)

1. **Windows, single process, no reverse proxy.** No Gunicorn, no Nginx, no systemd. Every
   multi-worker conclusion (rate limiting, in-memory state, connection-pool sizing) is derived
   from code reading, not measurement.
2. **SQLite, not PostgreSQL.** `_supports_row_level_locking()` (`app.py:2571-2572`) returns
   `False` on SQLite, so `SELECT … FOR UPDATE` is skipped in every test run. SQLite also does not
   enforce foreign keys by default. Two of the P0 findings below (F-05, F-06) are precisely the
   class of defect this makes invisible.
3. **No Redis, no real SMTP, no Razorpay test mode exercised.** `tests/conftest.py:69-83`
   monkeypatches both SMTP and `requests.Session.request` to raise on any unmocked external call —
   correct for hermetic tests, but it means no integration has ever been proven end-to-end here.
4. **No production data volume.** Every index/query judgement is structural, not measured.
5. **Full regression not completed within the audit window** — see Section 32 for the exact
   command, the observed rate, and the reason.

---

## 3. P0 Production Blockers

Nine items. Each is a defect with concrete evidence, not a missing feature. Missing V1.1
commercial features are tracked separately in Sections 7 and 31 — they block *V1.1 scope*, not
*production correctness of what exists*.

### P0-1 — Plan activation resets usage counters and destroys purchased scan entitlements
`app.py:8632-8640`

```python
user.subscribed_project_limit = reconciled_project_limit(user, plan.total_project_limit)
user.subscribed_scan_limit    = plan.total_scan_limit      # <- raw overwrite
user.projects_used = 0                                      # <- unconditional reset
user.scans_used    = 0                                      # <- unconditional reset
```

Three distinct failures in four lines:

* **Purchased `EXTRA_SCANS` are annihilated.** Projects are protected by
  `reconciled_project_limit()` (`app.py:2623-2627`), which re-adds the `PROJECT_CAPACITY` ledger.
  There is **no `reconciled_scan_limit()` counterpart.** The `EntitlementTransaction` rows survive
  as an audit trail whose materialised effect has been silently deleted. This directly violates
  the locked rule *"purchased reusable add-ons remain additive"* and is a straight revenue loss to
  the customer. The design comment at `app.py:2605-2607` mandates that every plan re-sync route
  through a reconciler — it was implemented for projects and omitted for scans.
* **Usage counters reset to zero on every paid activation**, including renewal or repurchase of
  the *same* plan. A user holding 18 real projects has `projects_used` set to 0 and may then
  create a full new plan allowance on top. `_reserve_project_quota_atomic` (`app.py:2659`) trusts
  this column exclusively, so the capacity gate is bypassed for real. The existence of the
  `reconcile-quota-counters` CLI (`app.py:3239-3262`) with its stored-vs-calculated drift report
  is itself evidence that this drift is known to occur.
* **Validity is not chained.** `subscription_end = now + duration` (`app.py:8605-8609`) discards
  unused remaining time on an early upgrade, whereas the `VALIDITY_EXTENSION` add-on correctly
  chains (`app.py:7813-7817`). The same block computes months as `duration_value * 30`, so a plan
  whose `duration_display` advertises **"1 Year"** (`models.py:92-93`) actually grants **360 days**.

Classification: `BLOCKER`, commercial integrity. Also `TEST-GAP` — there is no upgrade or
downgrade test anywhere in the suite.

### P0-2 — A CHECK constraint makes the project-coverage add-on impossible to insert
`migrations/versions/f4a8c2b91d70_addon_entitlement_foundation.py:34`

```python
sa.CheckConstraint("addon_type IN ('EXTRA_SCANS','VALIDITY_EXTENSION','PROJECT_CAPACITY')",
                   name="ck_addon_catalog_type")
```

`ADDON_TYPES` in `models.py:629` and `ADDON_PURCHASABLE_TYPES` in `app.py:7732` both include
`PROJECT_SERVICE_COVERAGE`, and the entire standalone-project-renewal product is built on it
(`app.py:7824-7833`, `ProjectServiceCoverage`, `PROJECT_SERVICE_COVERAGE_SOURCE_TYPES`). No later
migration drops or recreates this constraint. **On any migrated (i.e. production) database the row
cannot be inserted.** The test suite does not catch it because tests use `db.create_all()` and the
SQLAlchemy model carries no matching `__table_args__` CheckConstraint (`models.py:634-650`) — the
schema under test is *not* the schema that ships.

Classification: `BLOCKER`, `MIGRATION-REQUIRED`. This is also the single clearest argument in this
audit for adding a PostgreSQL/migrated-schema test lane (Section 32).

### P0-3 — The add-on catalogue is never seeded and has no Admin CRUD
Zero `AddonCatalog(` constructor calls exist outside `models.py` — not in `app.py`, not in any
migration, not in `scripts/`. `GET /api/addons/catalog` (`app.py:8303-8312`) filters on
`is_active=True, is_commercially_available=True` and will return `[]` on every freshly migrated
production database. The entire add-on, entitlement-ledger, and add-on-refund-reversal subsystem
is therefore dark in production, and there is no Admin route to populate it.

Classification: `BLOCKER`, `ADMIN-UI-CANDIDATE`, `SUPERADMIN-ONLY`.

### P0-4 — Deleting an admin-owned project deletes none of its files, silently
`app.py:3185-3210`

`_delete_project_files_and_rows()` hard-codes `IMAGES_DIR` / `VIDEOS_DIR` / `FEATURES_DIR` /
`QR_DIR` (lines 3188-3190, 3201). Admin-owned projects write to `ADMIN_IMAGES_DIR` etc.
(`app.py:681-684`, used at `7268-7269`, `7419`, `13142`, `processing_operations.py:8-11`).
`os.path.exists()` is therefore `False` for every path, nothing is unlinked, the DB rows are
deleted, and every image, video, `.npz` and QR file in `data_admin/` is orphaned permanently.
Both unlink loops swallow all errors with a bare `except Exception: pass` (lines 3195-3196,
3205-3206) with **no logging**, so the same silence also hides genuine permission/lock failures on
the user path. A correct directory-branching helper already exists three files away
(`processing_operations._dirs_for_project`) and is simply not used here.

Classification: `BLOCKER` (silent, permanent, unbounded storage leak; and it makes any future
storage-accounting reconciliation unsound from day one).

### P0-5 — Project deletion raises `IntegrityError` on PostgreSQL for any resumably-uploaded project
`models.py:1982-1983` declares `UploadSession.project_id` and `UploadSession.pair_id` as foreign
keys to `projects.id` / `project_pairs.id`. `Project` has **no** `upload_sessions` relationship and
no cascade (`models.py:909-915`), and `_delete_project_files_and_rows` never nulls or removes those
rows. SQLite does not enforce foreign keys by default, so every test passes. **PostgreSQL does**,
so the first production user who deletes a project created through the resumable-upload flow gets
a 500. This is a production-only, test-invisible hard failure on a routine user action.

Classification: `BLOCKER`, `SCHEMA-CHANGE-REQUIRED` (or an explicit pre-delete detach).

### P0-6 — The job queue silently degrades to a no-op mode, and `/ready` still returns 200
`processing_queue.py:34-51` resolves mode as: explicit `SCANSTORY_QUEUE_MODE` → `queue_required()`
→ `SCANSTORY_TESTING` → `REDIS_URL` present → **`fake`**. In `fake` mode
(`processing_queue.py:141-142`) the `ProcessingJob` row is created, `queue_job_id` is set to
`fake-N`, and **nothing ever runs**. `queue_available()` (`processing_queue.py:78-84`) returns
`True` unconditionally for `fake`/`inline`, and `_readiness_checks()` (`app.py:584-595`) only
probes Redis when `mode == "rq"` — so `/ready` reports **200 ready** while no upload is ever
processed. The production guard at `app.py:133-140` is real but depends entirely on one of
`SCANSTORY_PRODUCTION` / `APP_ENV` / `ENV` / `FLASK_ENV` carrying a production value
(`core/config.py:15-20`). A deploy that sets none of them boots happily into the dead-pipeline
state. Related: setting `SCANSTORY_TESTING=1` on a production host additionally permits SQLite
(`app.py:124,174`) and forces `fake` — and unlike `SCANSTORY_DEV_TESTING`, it is **not** in the
production prohibition list at `app.py:143-144`.

Classification: `BLOCKER`, `MISCONFIGURED`-by-default, `SERVER-TEAM-VERIFY` (the deployed env set
must be evidenced).

### P0-7 — No whole-request body cap exists at any layer that this repository controls
`app.py:3562-3568` leaves `MAX_CONTENT_LENGTH` **unset by default**, deliberately, because a
legitimate multi-pair upload can approach `MAX_VIDEO_SIZE × max_pairs_per_project` — with the
shipped defaults, **1 GiB × 10 = 10 GiB in a single request**. Per-file size is only checked
*after* the file has been fully spooled to disk (`upload_validation.py:77` → `80-86`), which is
unavoidable for multipart but means the only real ingest bound is the reverse proxy. No
`client_max_body_size` value appears anywhere in `docs/production/` — the only body-size guidance
in the repo concerns the *resumable chunk* route (`docs/production/README.md:76,94`;
`.env.example:27-29`). The resumable path is correctly bounded (413 at `app.py:7013-7018`); the
legacy multipart `/upload` path is not.

Classification: `BLOCKER` pending `SERVER-TEAM-VERIFY`. Either evidence a proxy body cap or set
`MAX_CONTENT_LENGTH` — but note that a fixed cap and a per-plan `max_video_bytes` interact, so
this should be resolved together with Section 7's media-policy work.

### P0-8 — Admin login and admin password reset have no rate limiting at all
`app.py:10847` (`POST /admin/login`) and `app.py:10895` (`POST /admin/forgot-password`) call
`_check_rate_limit` nowhere. Admin login is protected only by a per-email DB lockout (5 attempts /
15 minutes, `app.py:1473-1474`), which does nothing against distributed spray across many admin
emails and nothing against enumeration. Admin forgot-password is an unlimited authenticated-mail
trigger — `_create_otp` is called directly at `app.py:10904`, bypassing the `_resend_otp`
throttles. Compounding this, the limiter that *does* protect user login is process-local
(`rate_limit.py:1-7,13-63`), so under `gunicorn -w N` every published limit is silently `×N` and
is reset by every rolling restart. This is documented as a known gap
(`docs/production/README.md:102,122-123`; `security-proxy-checklist.md:19-23`) with
`RATE_LIMIT_REDIS_URL` reserved but **never read by any code**.

Classification: `BLOCKER` for the two unlimited admin routes; `P1-HARDENING` for the
process-local limiter (already documented and accepted for single-process V1, but not for V1.1
multi-worker production).

### P0-9 — The V1.1 coverage gate makes every admin-owned project permanently unavailable
`app.py:2110` — **surfaced by the one failing test in the full regression**

`project_public_access_state()` resolves the owner as:

```python
owner = User.query.get(project_current_owner_user_id(project)) if project_current_owner_user_id(project) else None
if owner and owner.has_active_subscription():
    best_source = "OWNER_SUBSCRIPTION"
```

`project_current_owner_user_id()` (`app.py:1763`) returns `current_owner_user_id or owner_user_id`.
For an **admin-owned project** both columns are NULL by construction (ownership is carried by
`owner_admin_id`), so `owner` is `None`, no `OWNER_SUBSCRIPTION` coverage is established, and — with
no `ProjectServiceCoverage` row either — `is_live` is `False`. **There is no branch anywhere in
`project_public_access_state` that considers `owner_admin_id`.**

Consequence: every admin-owned project is treated as out of coverage at all 13 enforcement
surfaces. `serve_admin_image` (`app.py:13316`), `serve_admin_video` (`:13339`) and `serve_admin_qr`
(`:13362`) all call `_project_is_available()` and return the unavailable response, so an entire
class of projects has non-functional media, QR codes, and scanner playback.

This is a regression introduced by the V1.1 ownership/coverage foundation (migration
`d2a4b6c8e0f1`), which modelled coverage purely in terms of *user* ownership. It is already caught
by an existing security test that **fails on the audited baseline** — the focused gate the brief
cites does not run it.

Note how this compounds P0-4: admin-owned projects are a second-class path that the V1.1 work did
not cover, and the two defects share that root cause. Fixing coverage without also fixing deletion
would leave admin projects serving correctly while still leaking every file on delete.

Classification: `BLOCKER`, functional regression, **currently failing in CI**.

---

## 4. P1 Production Hardening

| ID | Area | Finding | Evidence |
| --- | --- | --- | --- |
| P1-01 | Errors | Raw `str(e)` returned from **unauthenticated** endpoints, leaking SMTP banners and SQLAlchemy schema fragments | `app.py:5402` (contact form), `app.py:10472` (`/api/scanner/session/end`, also `@csrf.exempt` and `traceback.print_exc()`) |
| P1-02 | Errors | Raw `str(e)` from authenticated payment routes, leaking Razorpay API error bodies | `app.py:8710`, `8783`, `8793`, `8823` |
| P1-03 | Logging | `logging.basicConfig(level=logging.DEBUG)` unconditional in production; `LOG_LEVEL` documented but never read; no file handler, no rotation | `app.py:94-95`; `docs/production/README.md:71` |
| P1-04 | Logging | Structured telemetry produces **no output**: `_log_scanner_latency` / `_log_upload_timing` / `_log_processing_timing` pass rich dicts via `extra=` with no JSON formatter installed, so only the bare event name reaches stderr | `app.py:342`, `385`, `405` vs `app.py:94` |
| P1-05 | Logging | 156 `print()` calls in `app.py` used as a logging channel, bypassing levels and formatting; `_otp_log` writes user email addresses at INFO | `app.py:1227-1235`, `6048`, `8862`, `10913`, `5397` |
| P1-06 | Observability | No centralized error monitoring of any kind (`requirements.txt` has no Sentry/OTel/Datadog); no request/correlation IDs | `requirements.txt` (27 lines) |
| P1-07 | Readiness | `/ready` never checks RQ **worker liveness**, media-directory writability, or migration head vs Alembic head | `app.py:584-595`; zero `os.access`/`W_OK` hits in application code |
| P1-08 | Auth | Login lockout is keyed on `user_id` only and failures are never cleared on success → any anonymous party can lock any known account for 3 hours with 4 bad POSTs; a legitimate user stays locked after a successful login | `app.py:5894-5917` vs the admin path which correctly clears (`app.py:10883`) |
| P1-09 | Auth | `is_blocked` is not checked on the four resumable-upload routes; a user blocked mid-upload can still finalize and materialize a Project | `app.py:6992`, `7131`, `7489`, `7579`; `_upload_identity` at `app.py:6643-6656` |
| P1-10 | Auth | reCAPTCHA **fails open** when `RECAPTCHA_SECRET_KEY` is unset, and the key is not in the production required-config list | `app.py:415-418`, `264-265`, `114-156` |
| P1-11 | Session | `PERMANENT_SESSION_LIFETIME` is never set and `session.permanent` is never used → no session expiry and no idle timeout exist at all | absent from all `.py` |
| P1-12 | Session | No `session.clear()` / regeneration on login for either users or admins (defence-in-depth; becomes exploitable if a server-side session backend is ever introduced) | `app.py:6011`, `2492-2496` |
| P1-13 | AuthZ | `permissions_json` on `Admin` defaults to granting everything including `manage_admins` and is **never read** by any authorization check — a trap that silently promotes every existing admin if it is ever wired in | `models.py:783-786,809-812` vs `app.py:2214-2220` |
| P1-14 | AuthZ | Three high-value routes gate on bare `current_admin()` instead of `require_admin_permission(...)`, bypassing the RBAC layer (no privilege gain today, because role `admin` already holds the equivalent permissions — but the pattern defeats any future tightening) | `app.py:5111-5121` (dashboard `?admin_view`), `7660` (project view), `10821` (project preview) |
| P1-15 | Rate limit | Nine further mutating endpoints have no limiting: `/create-razorpay-order`, `/verify-payment`, `/api/addons/orders`, `/api/addons/purchases/<id>/verify`, all four `/api/uploads/sessions*` routes, `/send-contact-email` | `app.py:8663`, `8795`, `8315`, `8403`, `6882`, `6992`, `7489`, `7579`, `5328` |
| P1-16 | Media | `/admin/image`, `/admin/video`, `/admin/qr` carry no auth decorator while emitting `Cache-Control: private` — the header signals a restriction the route does not enforce | `app.py:13331`, `13333-13348`, `13350-13366` |
| P1-17 | Media | `serve_qr` skips the availability gate when the filename does not parse (`if project and not _project_is_available(project)`), so any file in `QR_DIR` is publicly readable and a suspended project's QR keeps serving if its name fails to parse | `app.py:9473-9479`, `2248-2260` |
| P1-18 | Upload | Client-side limits are hard-coded JS literals; any `MAX_IMAGE_UPLOAD_BYTES` / `MAX_VIDEO_UPLOAD_BYTES` / chunk-size env override silently desyncs the UI. `MAX_PAIRS_PER_PROJECT` proves the server-injection pattern already exists in the same file | `templates/user/user_create_project.html:3372`, `3898` vs `:2100` |
| P1-19 | Upload | Client accepts `video/quicktime`; a MOV carries an `ftyp` box, passes the server signature check, and is then stored and served with a hard-coded `.mp4` extension | `user_create_project.html:3381` vs `upload_validation.py:40-42,207` |
| P1-20 | Upload | Multi-pair edit is non-atomic: earlier `os.replace` calls persist while a later validation failure skips the commit, leaving files and DB rows divergent | `app.py:5567-5604` |
| P1-21 | Upload | `ImageFile.LOAD_TRUNCATED_IMAGES` is saved/restored around validation but is process-global and not thread-safe under the `ThreadPoolExecutor` | `upload_validation.py:112-113,144`; `app.py:262`, `3570` |
| P1-22 | Cleanup | `flask cleanup-upload-sessions`, `expire-stale-reservations`, `recover-processing-jobs`, and `reconcile-quota-counters` all exist and all work — **none is scheduled anywhere.** Nothing in the repo cron/timer-invokes them | `app.py:7608`, `3289`, `3350`, `3239` |
| P1-23 | Queue | `retry_failed_job` is imported at `app.py:77` and never called; `retry_eligible` is computed at `processing_queue.py:281` and nothing acts on it — there is no retry path for a failed job | verified by repo-wide grep |
| P1-24 | Queue | `rq_worker.py:29` runs `Worker(...).work(with_scheduler=False)` while enqueue configures `Retry(interval=[30,120,300,900])` — delayed retries require RQ's scheduler | `processing_queue.py:157-158` vs `rq_worker.py:29` |
| P1-25 | SMTP | All mail is sent synchronously in the request path with no retry; four of five failure sites use `print()`; no last-success/last-failure state is tracked anywhere | `app.py:2362-2426`, `_smtp_diagnostics_payload` at `12832-12856` |
| P1-26 | SMTP | `SMTP_SECURITY=none` is a permitted value with no production guard — credentials would cross the wire in plaintext | `core/config.py:57` vs `app.py:130-132` (presence check only) |
| P1-27 | SMTP | Contact form interpolates raw user input into the `Subject` header with no newline stripping, and into an HTML body via f-string with no escaping (the other four mail templates correctly use autoescaped `render_template`) | `app.py:5361-5386` → `2378` |
| P1-28 | Coverage | `ProjectServiceCoverage.EXPIRED` is in the status vocabulary but **never written by any code path**; there is no expiry sweeper, so rows stay `ACTIVE` past `coverage_end` forever and correctness rests entirely on read-time comparison | `models.py:856`; `app.py:1929-1937` |
| P1-29 | Refunds | Subscription refunds return money but revoke nothing — reconciliation is set to `MANUAL_REVIEW_REQUIRED` with no queue, alert, or Admin listing that surfaces those rows (only `GET /admin/api/refunds/<id>` by known id) | `app.py:8139-8145`, `12105` |
| P1-30 | Refunds | Refunds never release the launch capacity slot; a refunded account holds a `PaymentReservation` in `activated` forever | no refund path touches `CapacityConfig`/`PaymentReservation` |
| P1-31 | Refunds | Money moves before the DB records it: `REFUND_PROCESSING` is committed, then Razorpay is called; a crash in between leaves an orphan row recoverable only via the webhook, and unlike payments there is no `reconcile-refunds` CLI | `app.py:8260-8280` vs `app.py:9307` |
| P1-32 | Moderation | Report status transitions are unordered — a `DISMISSED` report can be flipped back to `ACTION_TAKEN` repeatedly, re-suspending the project each time | `app.py:12499-12500` |
| P1-33 | Moderation | Reporter hashes are salted with `app.secret_key`, so rotating the session secret orphans every existing hash and destroys the abuse-correlation value of the columns | `app.py:8471-8476` |
| P1-34 | Moderation | `content_reports` is `cascade="all, delete-orphan"` from `Project` — deleting a project destroys its moderation history | `models.py:1379` |
| P1-35 | Data | Deleting a `User` cascades to `payment_orders` (`models.py:197`) while `PaymentRefund.payment_order_id` has no matching cascade → financial-record destruction and a PostgreSQL FK failure. No user-deletion route exists today, which is the only reason this is latent | `models.py:190-199`, `577` |
| P1-36 | Tooling | `run-tests.ps1:9-14` hard-codes `$ExpectedRoot = "F:\ScanStory-main\ScanStory-main"` and exits 2 from any other worktree; lines 30/48 invoke bare `python` rather than the authoritative venv | `run-tests.ps1` |
| P1-37 | Docs | `docs/production/` is stale against V1.1: it states "there is no automatic refund flow" and "queue monitoring is future until Redis/RQ exists", both of which shipped | `README.md:124,131-132`; `monitoring-alerting.md:35` |
| P1-38 | Migrations | `bc5642a86981` converts two indexes to `UNIQUE` with a preflight that **raises** on existing duplicates and no cleanup path — the migration most likely to abort mid-upgrade on a populated production DB | `bc5642a86981:57-78` |

---

## 5. P2 / Post-Go-Live

| ID | Finding | Evidence |
| --- | --- | --- |
| P2-01 | The entire `Organization` / `Workspace` / `Experience` / `ExperienceVersion` / `Trigger` / `Asset` / `TriggerAsset` / `RecognitionArtifact` model tree ships in the schema behind eight feature flags that all default `False`. It is dormant weight — but see Section 9: `Asset(storage_provider, storage_key, size_bytes, status)` is very close to the media-ledger abstraction V1.1 needs | `models.py:1500-1783`; `feature_flags.py:4-13` |
| P2-02 | `compress_video` (`app.py:3882-3904`) has zero callers and produces `_stored.mp4`/`_compressed.mp4` while `ProjectPair.compressed_video_filename` looks for `_fast.mp4` — a naming mismatch that has never mattered because neither runs | dead code |
| P2-03 | `beneficiary_user_id` is declared with a relationship and is never read or written anywhere in `app.py` | `models.py:871,915` |
| P2-04 | `ProjectPair.image_file_path` / `npz_file_path` hard-code the user directories with no admin branch — the same root cause as P0-4, latent rather than live | `models.py:1119,1146` |
| P2-05 | `migrations/env.py:60-67,117-120` comments describe production as **MySQL**; the app now refuses to boot on anything but PostgreSQL | doc drift |
| P2-06 | `gate_d_build_rehearsal_db.py:70` hard-codes `Admin@123` / `admin@scanstory.test` — dev-only rehearsal tooling, off the app path, but a live credential string in the repo | |
| P2-07 | Duplicated plan seed: `app.py:986-1039` and `app.py:3647-3700` seed the same three plans; the second is **not** gated by `SCANSTORY_SKIP_STARTUP_BOOTSTRAP` and carries a stale comment | |
| P2-08 | CSP ships report-only by default and contains `'unsafe-inline'` for both script and style, so even when enforced it provides little XSS protection | `app.py:511-512,521,528` |
| P2-09 | User password minimum is 6 characters (admin reset requires 8); no complexity or breach check | `app.py:5719`, `6078`, `10939` |
| P2-10 | `/register` returns "Email is already registered" — a user-enumeration oracle mitigated only by the 30/hour IP limit | `app.py:5726` |
| P2-11 | `RQ_MAX_RETRIES` is read at `processing_queue.py:127` but appears in neither `.env.example` nor `docs/production/README.md` | |
| P2-12 | `.env.example:5` ships a literal `user:password@localhost:5432/scanstory_dev` DSN with no placeholder marker, unlike every other secret in the file | |
| P2-13 | A ₹0.50 plan would be charged ₹1 by the `max(100, …)` paise floor and then fail webhook amount reconciliation | `app.py:8705-8706` vs `9191-9198` |
| P2-14 | No `Idempotency-Key` is sent to Razorpay on `order.create`; a retried request creates a second Razorpay order | `app.py:8737` |

---

## 6. Complete Production Readiness Matrix

| Area | Status | Evidence | Risk | Required Action |
| --- | --- | --- | --- | --- |
| Flask app structure | `PARTIAL` | `app.py` 13,522 lines, all routes + domain + CLI in one module | Maintainability, merge contention | Section 29 — extract by seam, do not rewrite |
| Startup config validation | `PASS` | `app.py:114-156` fails fast on missing `FLASK_SECRET_KEY`, non-Postgres prod DB, missing SMTP/Redis in prod | — | Add `SCANSTORY_TESTING` to the prod prohibition (P0-6) |
| Secret handling | `PASS` | No hard-coded secrets; no `.env` tracked; `.gitignore` covers `.env*`, `*.pem`, `*.key`, `credentials.json` | — | Mark `.env.example:5` DSN as a placeholder (P2-12) |
| Debug mode | `PASS` | `app.py:166,198`, `app.run` only under `__main__` | — | — |
| Log level / rotation / structure | `MISCONFIGURED` | `app.py:94-95` DEBUG unconditional; no handler, no rotation, `extra=` payloads discarded | PII volume, unusable logs | P1-03, P1-04, P1-05 |
| Error monitoring | `MISSING` | no Sentry/OTel in `requirements.txt` | Blind in production | P1-06 |
| `/healthz` | `PASS` | `app.py:577-581` static 200 liveness, `no-store` | — | — |
| `/ready` | `PARTIAL` | `app.py:584-616` checks DB + Redis (only in `rq` mode) | Green while pipeline dead | P0-6, P1-07 |
| PostgreSQL enforcement | `PASS` | `app.py:123-128` rejects non-Postgres in production | — | — |
| Connection pool | `PARTIAL` | `app.py:175-194`: `pool_size=10`, `max_overflow=20`, `pool_pre_ping=True` → 30 conns/process | Exhaustion at N workers | `SERVER-TEAM-VERIFY` worker count + `max_connections` |
| Alembic chain | `PASS` | Single head `b2c4d6e8f0a1`, 15 linear revisions, all with real `downgrade()` | — | — |
| Migration safety | `PARTIAL` | `bc5642a86981` aborts on duplicates; `b7c9d2e4f6a1` downgrade overwrites NULLs with `''` | Mid-upgrade abort | P1-38; add duplicate preflight/cleanup |
| Migrated-schema vs model drift | `MISCONFIGURED` | `ck_addon_catalog_type` exists in migration, absent from model | Product broken in prod only | **P0-2** |
| Project/scan quota reservation | `PASS` | `app.py:2575-2592` single conditional UPDATE, no check-then-write | — | — |
| Pair quota reservation | `PARTIAL` | `app.py:2690-2704` `SELECT … FOR UPDATE` on the parent project, correct on Postgres; skipped on SQLite | Untestable today | Add a Postgres test lane (Section 32) |
| Launch capacity reservation | `PASS` | `app.py:2732-2773` atomic; release idempotent | TTL expiry is CLI-only | P1-22 |
| Razorpay order creation | `PASS` | Amount from DB (`app.py:8705`), currency from plan, client cannot influence price | — | P2-13, P2-14 |
| Razorpay signature verification | `PASS` | Browser `app.py:8816-8828` (ownership-bound); webhook HMAC over raw bytes pre-parse `app.py:9079-9096`, fails closed | — | — |
| Webhook idempotency | `PASS` | Insert-first DB unique index `app.py:8910-8941`, `ebeab1cf4ec9:56-59` | — | — |
| Payment activation atomicity | `PARTIAL` | Conditional UPDATE gate is correct (`app.py:8611-8630`); the entitlement effects are not | Revenue/capacity loss | **P0-1** |
| Refund states / idempotency | `PASS` | Four unique constraints + exactly-one-source CHECK (`models.py:564-574`) | — | — |
| Refund reversal completeness | `PARTIAL` | Add-ons reverse correctly; subscriptions do not; capacity never reverses | Manual toil, capacity leak | P1-29, P1-30, P1-31 |
| Content reporting | `PASS` | Anonymous, reason-validated, IP/session hashed, never stores raw IP | Salt rotation | P1-33 |
| Moderation queue | `PASS` | `/admin/moderation` + `/admin/reports*`, permission-gated, non-destructive suspension | Unordered transitions | P1-32 |
| Ownership / transfer / claim | `MISSING` (surface) | Tables, states, migration `d2a4b6c8e0f1` and helpers all ship; **zero HTTP routes** and no callers for accept/claim/approve | Locked V1.1 requirement unmet | Section 17 |
| Project public availability (user-owned) | `PASS` | `app.py:2099-2136` = `is_active` AND (owner subscription OR active coverage); enforced at 13 surfaces | — | — |
| Project public availability (admin-owned) | `MISCONFIGURED` | `app.py:2110` has no `owner_admin_id` branch → always out of coverage | Entire project class non-functional | **P0-9** |
| Expiry non-destructive | `PASS` | Expiry is a read-time comparison; no sweeper deletes anything | — | — |
| Upload validation | `PASS` | Magic bytes + decoder cross-check, server-generated filenames, zero-byte and malformed rejection, pixel cap before `img.load()` | Thread-safety of the truncated-images toggle | P1-21 |
| Request body cap | `MISSING` | `MAX_CONTENT_LENGTH` unset; no documented `client_max_body_size` | Disk exhaustion | **P0-7** |
| Video duration limit | `MISCONFIGURED` | `app.py:3560` defaults to `0` → `None` → check never runs | Locked plan differentiator unenforced | Section 14 |
| Resumable upload protocol | `PASS` | Row-locked chunk append, crash self-heal by truncate, idempotent duplicate chunks, atomic conditional finalize, `_BoundedFileView` zero-copy validation | — | — |
| Media serving | `PARTIAL` | `send_from_directory` with correct Range/206 support; public by design; enumerable integer IDs | Admin routes undecorated | P1-16, P1-17 |
| Project deletion | `MISCONFIGURED` | Wrong dirs for admin projects; silent unlink failures; FK orphan | **P0-4**, **P0-5** | |
| Orphan reclamation | `MISSING` | No reconciliation of `data/*` against the DB anywhere | Unbounded, undetectable growth | Section 9 |
| Per-account storage accounting | `MISSING` | No column, no ledger, no sum query in the entire product | Locked V1.1 model unimplementable | Section 9 |
| Rate limiting | `MISCONFIGURED` | Process-local dict (`rate_limit.py:13-63`); `RATE_LIMIT_REDIS_URL` reserved but never read | ×N workers | **P0-8**, P1-15 |
| CSRF | `PASS` | Global `CSRFProtect`; all 7 exemptions justified; every POST form carries a token | Report route caveat | Section 15 |
| IDOR | `PASS` | One shared `user_can_manage_project()` helper applied consistently; 404 not 403 | Three bare-`current_admin()` routes | P1-14 |
| Open redirect | `PASS` | Zero `next`/`redirect(request.…)` anywhere; `form-action 'self'` | — | — |
| Blocked-user enforcement | `PARTIAL` | Live-DB `current_user()` makes blocking effective immediately on `@login_required` routes | Four upload routes uncovered | P1-09 |
| Admin RBAC | `PASS` | `ADMIN_ROLE_PERMISSIONS` + `require_admin_permission`, last-superadmin protection with `FOR UPDATE` | Phantom `permissions_json` | P1-13 |
| Session cookie flags | `PASS` | HttpOnly, SameSite=Lax, Secure required in prod | No lifetime at all | P1-11 |
| Backup / restore tooling | `MISSING` (code) / `PASS` (docs) | `docs/production/backup-restore-runbook.md` is thorough prose; **no `pg_dump`, no media sync, no restore script exists** | Unverifiable | `SERVER-TEAM-VERIFY` |
| Gunicorn / Nginx / systemd | `MISSING` | Zero config files of any kind in the repository | Cannot certify | `SERVER-TEAM-VERIFY` (all of Section 33) |
| Test suite | `PARTIAL` | 1489 tests, 0 collection errors, broad V1 coverage | **SQLite-only**; zero V1.1 commercial coverage | Section 32 |

---

## 7. V1.1 Commercial Model Gap Matrix

`E` = exists, `P` = partial, `M` = missing. `S` = schema change required, `B` = backend
enforcement required, `A` = Admin UI required, `U` = user-facing UI required, `T` = tests required.

| # | Locked requirement | Existing | Partial | Missing | Schema | Backend | Admin UI | User UI | Tests |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Plan family INDIVIDUAL / BUSINESS_VENDOR | `User.account_type` (`models.py:140-145`) | plan side | **plan has no family column** | S | B | A | U | T |
| 2 | Price / offer price / currency | `plan_amount`, `offer_price`, `currency` (`models.py:37-39`) | — | — | — | — | exists | exists | partial |
| 3 | Billing term / validity | `duration_type`, `duration_value`, `trial_days` (`models.py:42-46`) | month≈30d bug | — | — | B (fix `*30`) | exists | exists | T |
| 4 | Availability / publish state | `is_active` (`models.py:54`) | boolean only | draft / grandfathered / disabled-for-new-purchase / archived | S | B | A | — | T |
| 5 | Plan version / revision | — | — | **absent** — `admin_edit_plan` mutates live rows (`app.py:11642-11760`) | S | B | A | — | T |
| 6 | Active project capacity | `total_project_limit` + `PROJECT_CAPACITY` ledger + `reconciled_project_limit()` | — | — | — | — | exists | exists | partial |
| 7 | Scan allowance | `total_scan_limit` + `EXTRA_SCANS` ledger | **no `reconciled_scan_limit()`; wiped on activation** | — | — | **B (P0-1)** | exists | exists | T |
| 8 | Max pairs per project | `max_pairs_per_project` (`models.py:35`), active-count semantics, `FOR UPDATE` reservation | — | — | — | — | exists | exists (injected) | good |
| 9 | Max trigger image size | — | — | **absent from DB**; global `MAX_IMAGE_SIZE` env (`app.py:3555`) | S | B | A | U | T |
| 10 | Max video size | — | — | **absent from DB**; global `MAX_VIDEO_SIZE` env (`app.py:3556`) | S | B | A | U | T |
| 11 | Max video duration | — | code path exists (`upload_validation.py:195-202`) | **disabled by default** (`app.py:3560`) and not plan-aware | S | B | A | U | T |
| 12 | Max image dimensions / pixels | — | global `MAX_IMAGE_DIMENSION_PX` / `MAX_IMAGE_PIXELS` | not plan-aware | S | B | A | U | T |
| 13 | Base total account storage | — | — | **does not exist anywhere in the product** | S | B | A | U | T |
| 14 | Reusable storage add-on | — | — | **absent**: `ADDON_TYPES` has no storage type; `AddonCatalog` has no `storage_delta` | S | B | A | U | T |
| 15 | Experience entitlement — Direct QR | `Project.experience_type` per project | gated by a code flag `direct_qr_experience_supported()` (`app.py:3024`) | **not a plan entitlement** | S | B | A | U | T |
| 16 | Experience entitlement — Detect Once | `Project.playback_mode` per project | — | **not a plan entitlement** | S | B | A | U | T |
| 17 | Experience entitlement — Tracked Overlay | `Project.playback_mode` per project | — | **not a plan entitlement** | S | B | A | U | T |
| 18 | Vendor / customer project management | `manager_vendor_user_id`, `is_business_vendor()`, `user_can_manage_project()` | — | **no plan-level capability flags**; no HTTP surface for transfer/claim | S | B | A | U | T |
| 19 | Ownership transfer capability | tables + helpers + `PENDING_CAPACITY` | — | **zero routes; accept/claim/approve have no callers** | — | **B** | A | U | T |
| 20 | Grandfathered media / playback | — | — | **no representation at all** (zero `grandfather` hits repo-wide) | S or derived | B | A | U | T |
| 21 | `OVER_PROJECT_CAPACITY` | `project_capacity_summary().over_capacity` computed, not persisted — this is the right shape | — | not surfaced to the user | — | B | A | U | T |
| 22 | `OVER_STORAGE` / `OVER_PAIR_LIMIT` | — | — | absent | — | B | A | U | T |

### Reading of the gap

The commercial model splits cleanly into three tiers of effort:

* **Tier 1 — already correct, leave alone.** Project capacity (base + purchased ledger +
  materialised column + atomic reservation) and pair capacity (plan-derived, active-count,
  row-locked) are both implemented the way the locked rules describe. `project_capacity_summary()`
  computing `over_capacity` rather than persisting an enum is exactly the "minimal representation"
  the brief asks for, and should be the template for `OVER_STORAGE` and `OVER_PAIR_LIMIT`.
* **Tier 2 — additive columns on an existing, healthy pattern.** `plan_family`,
  `max_image_bytes`, `max_video_bytes`, `max_video_duration_seconds`, `max_image_pixels`,
  `base_storage_bytes`, three experience-entitlement booleans, and a plan lifecycle enum are all
  plain columns on `SubscriptionPlan`. The enforcement seam already exists — every media limit is
  currently read from a module constant at four call sites (`app.py:5576`, `6232`, `7221`,
  `13086`); routing those through an effective-entitlement resolver is a contained change.
* **Tier 3 — genuinely new architecture.** Account storage. There is no accounting to extend.
  See Section 9.

### `SubscriptionPlan` can support this — with one caveat

The existing schema is a flat, additive table with no polymorphism and no versioning. Adding
Tier-2 columns is safe and does not require a new table. **The caveat is that plan rows are
currently the live source of commercial truth and are edited in place.** Adding
`max_video_bytes` to a table an admin can mutate at will, with thousands of subscribers reading
it, is the point at which plan versioning stops being optional (Section 8, "Plan governance").

---

## 8. Upgrade / Downgrade / Grandfathering Audit

Every locked rule, evaluated against actual code.

### Upgrade behaviour

| Locked rule | Verdict | Evidence |
| --- | --- | --- |
| Project capacity increases | `PASS` | `app.py:8637` via `reconciled_project_limit()` — purchased capacity preserved |
| Scan allowance increases | `FAIL` | `app.py:8638` raw overwrite destroys purchased `EXTRA_SCANS` (**P0-1**) |
| Base account storage increases | `N/A` | storage does not exist |
| Max pairs per project increases | `PASS` | derived live from `plan.max_pairs_per_project` at each check (`app.py:2986-3000`) — no materialised copy to go stale |
| Image / video upload limits increase | `FAIL` | limits are process-global env constants, identical for every plan |
| Video duration limit increases | `FAIL` | same, and disabled by default |
| Newly entitled playback modes become selectable | `N/A` | modes are not plan-entitled |
| Vendor capabilities change per plan | `N/A` | no plan-level vendor capability exists |
| Purchased add-ons remain additive | `PARTIAL` | true for `PROJECT_CAPACITY`, false for `EXTRA_SCANS` |
| Higher limits apply to future actions across existing and new projects | `PASS` for pairs (live read) | |
| Existing media not destructively regenerated | `PASS` | nothing in the activation path touches media |
| Atomic | `PARTIAL` | the status gate is a single conditional UPDATE and all effects land in one commit (`app.py:8611-8657`); the effects themselves are wrong, not the transaction |

**Additional upgrade defect:** `projects_used`/`scans_used` reset to 0 (P0-1) means the locked
statement "higher limits apply to future actions" is implemented as "the meter is reset", which is
strictly more generous than intended and breaks the capacity gate.

### Downgrade behaviour

**There is no downgrade flow.** No route lowers a user's plan. The nearest equivalents are
`admin_deactivate_subscription` (`app.py:11947-11968`, sets `subscription_end` to yesterday and
status to `expired`) and `admin_edit_plan` (`app.py:11642-11760`, mutates the plan row in place).

| Locked rule | Verdict | Evidence |
| --- | --- | --- |
| Prefer effective at next billing period | `MISSING` | no scheduled-change concept exists; any future implementation starts from zero |
| Do **not** delete projects | `PASS` (by absence) | `_delete_project_files_and_rows` has exactly four callers (`app.py:3519`, `5537`, `12409`, `13429`), none commercial |
| Do **not** delete pairs / images / videos / QR | `PASS` (by absence) | same |
| Do **not** truncate media | `PASS` | no truncation path exists |
| Do **not** silently convert playback modes | `PASS` | `playback_mode` is written only at project creation and never rewritten by any commercial path |
| Existing content remains intact | `PASS` | verified by call-site enumeration |
| Lower-plan rules apply to future actions | `PASS` for pairs (live read); `N/A` for media limits (not plan-scoped) | |
| `OVER_PROJECT_CAPACITY` representable | `PASS` | `project_capacity_summary()` computes `over_capacity = used > limit` (`app.py:2647`) without persisting an enum — correct minimal shape, currently not surfaced |
| `OVER_STORAGE`, `OVER_PAIR_LIMIT`, `GRANDFATHERED_*` | `MISSING` | no representation |

**Project capacity after downgrade (old 25 → usage 18 → new 10):** the current code would behave
correctly *if* a downgrade existed — `_reserve_project_quota_atomic` blocks new consumption once
`projects_used >= subscribed_project_limit` and nothing deletes. The one thing that breaks it is
P0-1: if the user later buys anything, `projects_used` resets to 0 and they are handed a fresh
allowance on top of 18 real projects.

**Storage after downgrade:** unimplementable today.

**Pair limit after downgrade (Project A has 8, new max 5):** the enforcement shape is already
correct. `_reserve_pair_slots_for_project` (`app.py:2697-2704`) compares
`existing_pairs + requested > max_pairs`, so adding pair #9 is blocked while the existing 8 are
untouched, and deleting pairs restores headroom naturally because the count is a live
`COUNT(*)`. **What is missing is replacement:** there is no per-pair delete route at all, and pair
replacement (`app.py:5556-5604`) overwrites in place without going through
`_reserve_pair_slots_for_project` — correct today (count is unchanged, which matches the locked
rule) but it also means replacement media is validated only against the *global* constants, not
against any plan policy.

**Media after downgrade (keep 420 MB, allow 180 MB replacement, block 420 MB replacement):**
`KEEP` and `PLAY` pass by absence of any destructive path. `REPLACE` cannot yet distinguish the
180 MB and 420 MB cases because the size check is against a single global constant. Once
`max_video_bytes` becomes plan-scoped, `app.py:5593-5601` is the single correct enforcement point
for the "replacement is a new media action" rule.

**Playback entitlement after downgrade:** `Project.playback_mode` is persisted per project and is
never rewritten, so a grandfathered Tracked Overlay project *is* structurally safe from silent
conversion today. The two missing halves are (a) a plan-level entitlement to check when *creating*
or *changing* a project, and (b) protection of the existing right during ordinary metadata edits —
`user_edit_project` (`app.py:5556`) should be audited for `playback_mode` writes when that
entitlement lands.

### Grandfathering vs service coverage

The codebase already keeps these correctly separate, and this is a genuine strength worth
protecting:

* **Service coverage** is `ProjectServiceCoverage` + `project_public_access_state()`
  (`app.py:2099-2132`), evaluated as `is_active AND (owner subscription active OR an ACTIVE
  coverage row)`, enforced at 13 public surfaces.
* **Feature eligibility** is the plan/limit layer, evaluated only on write paths.

No place was found that conflates them. `LEGACY_COMPATIBILITY` coverage
(`PROJECT_SERVICE_COVERAGE_SOURCE_TYPES`) is a coverage source, not a feature grant, and
`test_domain_commercial_capacity_and_reporting.py:447` explicitly asserts that indefinite legacy
coverage **blocks** paid renewal rather than granting features. When grandfathering is
implemented, it must be added as a *feature-eligibility* concept and must not create or extend a
coverage row.

**One coverage anomaly to fix alongside:** `_project_specific_coverage_candidates`
(`app.py:1929-1937`) filters by `project_id` only and never re-derives the owner, so coverage
created under a previous owner keeps a project live for a new owner after transfer. Whether that
is intended (`TRANSFER_CARRY_OVER` exists as a source type, suggesting it is) needs a product
decision — but today it happens for *all* coverage sources, not only carry-over.

### Individual ↔ Business/Vendor conversion

`User.account_type` is a plain validated string column (`models.py:140-145,297-299`). No
conversion flow exists in either direction, and no guard exists. Given that a `BUSINESS_VENDOR`
account can hold `manager_vendor_user_id` on projects it does not own, converting one to
`INDIVIDUAL` without resolving those relationships would strand projects whose only manager is no
longer a vendor (`user_can_manage_project` requires `is_business_vendor(user)`,
`app.py:1777`). Recommended posture, for later implementation: **Individual → Vendor** is a safe
additive activation; **Vendor → Individual** must be blocked while any managed project, active
transfer, or open claim exists. Do not implement speculative conversion logic before the transfer
HTTP surface exists (Section 17).

---

## 9. Storage Architecture Audit

### Actual media storage model

**Local filesystem only. No database BLOBs, no object storage, no CDN.** Roots are defined at
`app.py:663-686`:

| Asset | User directory | Admin directory | Naming |
| --- | --- | --- | --- |
| Marker image | `data/images` (`:664`) | `data_admin/images` (`:681`) | `{project_id}_{pair_index}.jpg` |
| Video | `data/videos` (`:665`) | `data_admin/videos` (`:682`) | `{project_id}_{pair_index}.mp4` |
| ORB features | `data/features` (`:666`) | `data_admin/features` (`:683`) | `{project_id}_{pair_index}.npz` |
| QR PNG | `data/qr_codes` (`:667`) | `data_admin/qr_codes` (`:684`) | `project_{id}_main.png` |
| Resumable temp | `data/tmp_uploads` (`:675`) | shared | `resumable_{uuid4}.part` |

`DATA_DIR` defaults to the **relative** string `"data"` (`app.py:663`), overridable by
`SCANSTORY_DATA_DIR`. A relative default resolves against the process CWD, which under a
supervisor is not guaranteed — `docs/production/README.md:62` correctly marks the variable
required in production, but the code-level default is unsafe. `SERVER-TEAM-VERIFY`.

Model fields holding locations: `ProjectPair.image_filename` / `video_filename`
(`models.py:1042-1043`), `ProjectPair.image_path` and `Project.qr_code_path` (`:1044`, `:878` —
these hold **URLs**, not filesystem paths), `Project.qr_code_filename` (`:879`). Filesystem paths
are computed properties (`models.py:1117-1147`) that hard-code the *user* directories with no admin
branch (P2-04).

### Media accounting — the source of truth does not exist

`MISSING`. An exhaustive search for `storage_bytes`, `storage_used`, `bytes_used`,
`total_storage`, `storage_quota`, `disk_usage`, and any `func.sum` over a size column returns
**zero accounting constructs** in the entire product. Every quota in ScanStory is a *count*
(projects, scans, pairs); none is a *byte total*.

The only byte data that exists is per-pair and is not trustworthy as an aggregate:

* `ProjectPair.image_size` / `video_size` are `nullable=True` (`models.py:1048-1049`), and
  `image_size` is always NULL for `direct_qr` pairs.
* The two ingest paths populate them from different sources. The resumable path uses
  `os.path.getsize` (`app.py:7337`); the multipart path uses
  `marker_meta["processed_size_bytes"] or image_file.content_length` (`app.py:6343`) — a
  **client-supplied** value as fallback.
* **Neither column is updated on pair replacement** (`app.py:5556-5604` writes the file via
  `os.replace` and touches no size column), so the value goes stale on every replacement.
* `.npz` feature artifacts, QR PNGs, `_work.jpg` intermediates, and abandoned `.part` files are
  counted nowhere.
* Both columns are `db.Integer` — 4-byte signed on PostgreSQL, capping near 2.1 GB. The shipped
  1 GiB video ceiling fits; any future increase does not. The same applies to
  `EntitlementTransaction.delta_value` (`models.py:699`), which would overflow if ever used to
  carry storage byte deltas — a storage entitlement must use `BigInteger`.

**Verdict: account storage usage cannot be calculated reliably from the current schema.** A
filesystem walk is the only accurate method today, and it cannot attribute bytes to an account
without re-deriving ownership from filenames.

### Recommended shape (audit recommendation only — do not implement yet)

A media-object/ledger abstraction is warranted, and **a very close approximation already exists in
the dormant schema**: `Asset` (`models.py:1716-1737`) carries `workspace_id`, `asset_type`,
`storage_provider`, `storage_key`, `original_filename`, `mime_type`, `size_bytes`, `status`, and
timestamps — precisely the field set the brief describes, plus a provider field that would make a
future object-storage migration a data change rather than a code change. It is inert behind
`ENABLE_EXPERIENCE_CREATOR=False` and is keyed to `Workspace` rather than `User`.

Two defensible options, to be decided before implementation:

1. **New `media_object` table keyed to account + project + pair**, with `owner_user_id`,
   `project_id`, `pair_id`, `media_type`, `storage_key`, `logical_bytes` (`BigInteger`),
   `physical_bytes`, `status` (`ACTIVE` / `PENDING_DELETE` / `DELETED`), optional `checksum`,
   `created_at`, `deleted_at`. Storage usage becomes
   `SUM(logical_bytes) WHERE owner_user_id = ? AND status = 'ACTIVE'`.
2. **Revive and re-key `Asset`.** Less net new schema, but it couples V1.1 commercial correctness
   to a subsystem that is disabled and untested at runtime, and inherits a `Workspace` FK with no
   V1.1 meaning.

Recommendation: **option 1.** Keep `Asset` dormant. The coupling risk outweighs the schema saving,
and a `PENDING_DELETE` status is precisely what makes the deletion rules below implementable.

A materialised `User.storage_bytes_used` counter maintained by the same atomic conditional-UPDATE
pattern already used for `projects_used` (`app.py:2575-2592`) is the natural enforcement column,
with the ledger as the audit/reconciliation source — mirroring the project-capacity design that
already works.

### Deletion and reuse

| Locked rule | Current state |
| --- | --- |
| Permanent deletion returns capacity only after cleanup succeeds | **Unimplementable today.** `_delete_project_files_and_rows` (`app.py:3185-3210`) unlinks with `except Exception: pass` then commits unconditionally. A `PENDING_DELETE` status plus a verified-unlink step is required. |
| Archive / suspend / deactivate must NOT free storage | `PASS` by construction — suspension sets `project.is_active = False` only (`app.py:12515-12519`, `12371-12383`) |
| Project expiry must NOT delete media | `PASS` — expiry is a read-time comparison in `project_public_access_state` (`app.py:2099-2132`); no sweeper exists |
| Refund must NOT delete unrelated media | `PASS` — verified by enumerating all four callers of `_delete_project_files_and_rows` (`app.py:3519`, `5537`, `12409`, `13429`); no refund path reaches it |
| Replacing large media with smaller frees the difference | `MISSING` — replacement is `os.replace` in place (`app.py:5583`, `5601`) and updates no size column |
| Project delete removes image / video / QR / chunks / derived files | `PARTIAL and BROKEN`. Removes image, video, `.npz`, QR **for user projects only** (P0-4). Never removes `_fast.mp4` compressed variants, `_work.jpg` intermediates, or `.part` files. |
| DB deletion can succeed while physical cleanup fails silently | **CONFIRMED twice** — the bare `except: pass` (`app.py:3195-3196`, `3205-3206`) and the wrong-directory bug (P0-4) |

**Ordering note:** files are unlinked *before* `db.session.commit()` (`app.py:3209`). This is the
safer ordering for orphan prevention, but it inverts the failure mode: a commit failure after
successful unlinks leaves DB rows pointing at deleted files.

### Logical entitlement vs temporary physical headroom

The brief's distinction is directly relevant to replacement. Because replacement is an in-place
`os.replace`, ScanStory currently uses at most `old + new` bytes transiently in `TMP_UPLOADS_DIR`
and never double-counts logically (there is no logical counter at all). When storage accounting
lands, the correct rule is: **evaluate the projected logical total as
`current - old_media_bytes + new_media_bytes`**, so a 420 MB → 180 MB replacement is never rejected
for an over-storage account. Temporary physical headroom is a *server* concern
(`SERVER-TEAM-VERIFY`: free space on the media/temp mount), not a plan entitlement, and must not be
conflated with it.

### Orphan handling

`MISSING` entirely. There is no reconciliation anywhere that scans `data/images`, `data/videos`,
`data/features`, `data/qr_codes`, `data_admin/*`, or `data/tmp_uploads` against the database. The
only cleanup command, `flask cleanup-upload-sessions` (`app.py:7608-7647`), covers `.part` files
only and is **never scheduled** (P1-22). Every orphan source identified in this audit — silent
unlink failure, the admin-directory bug, abandoned `.part` files, stale `.npz` after a failed
reprocess — is permanent and undetectable.

### Storage and transfer

`accept_project_ownership_transfer` (`app.py:1853-1856`) checks recipient **project capacity** via
`_reserve_project_quota_atomic` and parks the transfer in `PENDING_CAPACITY` on failure. It checks
**no storage** (none exists) and **no subscription status** — a project can transfer to an expired
account, after which `project_public_access_state` reads the *new* owner's subscription and the QR
goes dark immediately unless project-specific coverage exists.

On whether `PENDING_CAPACITY` can represent both project and storage insufficiency: it can, but it
should not silently. The recipient's remediation differs (buy project capacity vs. free or buy
storage), and the state is already surfaced to users as *"Recipient needs an available project
slot"* (`app.py:1591`). Recommendation: keep the single persisted state and add a **reason/detail
field**, satisfying the brief's "do not invent additional persisted state unless evidence requires
it".

### Backup implications

`docs/production/backup-restore-runbook.md` correctly scopes backups to the database *plus* marker
images, videos, feature artifacts, QR assets, secrets, and the Alembic version. **No executable
backup tooling exists** — no `pg_dump` invocation, no media sync, no restore script, no filename
convention. The runbook's own line 3 (*"Do not claim a backup exists unless it has been
verified"*) is the correct posture. Everything here is `SERVER-TEAM-VERIFY`.

Critically, because there is no media ledger, **a restore cannot currently be validated**: there is
no manifest against which to confirm that every DB-referenced file came back.

---

## 10. Pair Capacity Audit

This is the healthiest part of the commercial model and should be the template for the rest.

**Representation.** `ProjectPair` (`models.py:1035-1093`), identified by
`UniqueConstraint("project_id", "pair_index")` (`:1089`) with supporting indexes at `:1090-1092`.

**Limit source.** `SubscriptionPlan.max_pairs_per_project`, `db.Integer, default=10`
(`models.py:35`) — **plan-derived, not an env constant**, admin-editable, minimum 1 enforced at
`app.py:11583` and `11726`. Resolved live per request by `get_plan_pairs_limit(user)`
(`app.py:2986-3000`).

**Active count, not lifetime.** `_reserve_pair_slots_for_project` (`app.py:2697-2704`) evaluates
`ProjectPair.query.filter_by(project_id=…).count() + requested > max_pairs` — a live `COUNT(*)`.
Deleting pairs restores headroom automatically. This exactly matches the locked rule.

**Concurrency.** `_lock_project_for_pair_quota` (`app.py:2690-2694`) takes `SELECT … FOR UPDATE` on
the parent `Project` row when `_supports_row_level_locking()` is true (PostgreSQL / MySQL /
MariaDB, `app.py:2571-2572`), serialising concurrent pair additions to the same project. Both
callers hold that lock through the subsequent inserts and the commit — verified at `app.py:6289`
(lock → check → insert loop `6293-6363` → commit `6376`) and `app.py:7311` (lock → check → insert
`7344` → commit `7349`). **Correct on PostgreSQL.**

Three caveats:

* On SQLite the lock is silently skipped, so the check-then-insert is unserialised. Since the test
  suite is SQLite-only, **this path has never been exercised under contention** — `TEST-GAP`.
* The function is named `_reserve_*` but reserves nothing; it only checks. Safety derives entirely
  from the caller holding the lock. Any future caller that checks and commits separately races.
* No `(project_id, count)`-level constraint backstops the check; the unique constraint prevents
  duplicate indices but not an over-limit count.

**Sentinel inconsistency.** `max_pairs is None` means **unlimited** inside
`_reserve_pair_slots_for_project` (`app.py:2698-2699`) but means **misconfigured, block the user**
at the route level (`app.py:6116-6118`, `6203-6205`, `6936-6942`). The routes fail closed, so there
is no live hole — but one sentinel carrying two opposite meanings is exactly the shape that
produces a hole after a refactor. `P1-HARDENING`.

**Deletion and reuse.** There is **no per-pair delete route at all.** Pairs are removed only as part
of whole-project deletion or via the ORM `cascade="all, delete-orphan"` on `Project.pairs`
(`models.py:909`). The locked example (*max=5, delete one, one slot becomes reusable*) is therefore
**not reachable by a user today** — the mechanism is correct, the surface is missing.

**Replacement.** `user_edit_project` (`app.py:5556-5604`) is a genuine overwrite-in-place: validate
into a temp file first (`5575-5577`, `5593-5595`), and only on success `os.replace(tmp, live)`
(`5583`, `5601`) — an atomic same-filesystem rename. Old media is never removed before the
replacement validates, there is no window with no media, and `pair_index` is reused so the pair
count is unchanged. This satisfies the locked replacement-safety contract for the *pair-count*
dimension. What it does **not** yet do is (a) validate against plan-scoped media policy rather than
global constants, (b) update `image_size` / `video_size`, or (c) behave atomically across multiple
pairs in one request (P1-20).

**Generation behaviour.** Replacing one pair does **not** regenerate the project. Reprocessing is
scoped by `pairs_to_process = [p for p in pairs if not p.is_processed]` (`app.py:5610`), and
`is_processed=False` is set only on pairs that were actually replaced (`:5585`). The QR is **not**
regenerated — `scanner_url` and `qr_code_filename` are project-scoped (`app.py:7440-7442`) and the
QR encodes only `project_id`, so printed codes survive any media change. Correct design; preserve
it. The one coarse behaviour is `load_features.cache_clear()` (`app.py:5646`, `3210`), which
flushes the process-wide feature LRU for *all* projects.

**Upload-session awareness.** A resumable session covers exactly one single-pair project
(`models.py:1919-1929`); the pair check happens at finalize (`app.py:7310-7313`) inside the same
locked transaction as the insert. Correct.

---

## 11. Effective Entitlement Architecture Findings

There is **no** `get_effective_entitlements(user)`. There is one *partial* centraliser, for project
capacity only, and it is well built:

```
app.py:2610  purchased_project_capacity(user)      # SUM of PROJECT_CAPACITY ledger deltas
app.py:2623  reconciled_project_limit(user, base)  # base plan + purchased
app.py:2630  effective_project_limit(user)         # the one number every project check must use
app.py:2643  project_capacity_summary(user)        # effective/purchased/base/used/remaining/over_capacity/unlimited
```

Everything else is scattered across at least ten independent sites:

| Concern | Location | Note |
| --- | --- | --- |
| Project capacity | `app.py:2610-2656` | the good one |
| Generic limit predicate | `_limit_reached()` `app.py:2549` | |
| Atomic project consume | `_reserve_project_quota_atomic()` `app.py:2659` | |
| Atomic scan consume | `_consume_scan_quota_atomic()` `app.py:2673` | |
| Redirect-style page gate | `check_user_limits()` `app.py:3102-3162` | uses `effective_project_limit()` for projects but **raw `user.subscribed_scan_limit`** for scans (`:3130`, `:3152`) |
| Pairs per project | `get_plan_pairs_limit()` `app.py:2986` + `_reserve_pair_slots_for_project()` `app.py:2697` | |
| Duplicate model-side limits | `User.can_create_project` / `can_scan` / `remaining_projects` / `remaining_scans` — `models.py:236-261` | reads `subscribed_project_limit` **directly**, bypassing `effective_project_limit()` |
| Subscription-active check | `User.has_active_subscription()` `models.py:208` | |
| Media limits | module constants `app.py:3555-3560`, enforced at `5576`, `5594`, `6232`, `6239`, `6912`, `6914`, `7221`, `7234`, `13086`, `13093` | not plan-aware at all |
| Project public availability | `project_public_access_state()` `app.py:2099` | correctly separate — coverage, not entitlement |
| Experience-mode gating | `direct_qr_experience_supported()` `app.py:3024` | a code/env flag, not an entitlement |
| Admin grants | `app.py:11466`, `11907`, `12679`, `12708` | write `subscribed_*` columns **directly**, bypassing the ledger — no source, no expiry, no entitlement audit |

### Three concrete duplication hazards

1. **Four mutually incompatible "unlimited" sentinels for one concept.**
   `effective_project_limit()` returns `None` (`app.py:2639`); `User.remaining_projects` returns the
   magic number `999999999` (`models.py:253`); `templates/user/profile.html:656,670,686,700` tests
   for `999999`; and `fix_limits.py` exists solely to convert `999999` rows to `NULL`. Worse, a plan
   row with `total_project_limit = 0` — the value an admin would naturally enter to mean "none" — is
   interpreted as **unlimited** (`models.py:252`, `app.py:2638`, `app.py:12696`), and
   `admin_update_scan_limit` explicitly accepts `>= 0` (`app.py:12687`). This is a live
   foot-gun in the Admin UI today.
2. **Scans are the systematically neglected twin of projects.** Projects have a reconciler, a
   summary function, and ledger preservation. Scans have none of those, are overwritten on
   activation (P0-1), and are gated by a raw column read in `check_user_limits`.
3. **Admin grants are invisible to the ledger.** Four routes mutate `subscribed_*` directly. The
   locked requirement that Admin sees *"PLAN BASE + PURCHASED ADD-ONS + ADMIN GRANTS + CURRENT
   USAGE = EFFECTIVE ENTITLEMENT"* is unachievable while grants leave no `EntitlementTransaction`
   row. `ENTITLEMENT_TYPES` would need an admin-grant source type, and
   `EntitlementTransaction.expires_at` — which **exists in the schema and is written by no code
   path** (`models.py:705`) — is exactly the field a "finite governed Admin grant" requires.

### Convergence target

`get_effective_entitlements(user)` returning a single frozen structure — plan base, purchased ledger
deltas, admin grants, materialised enforcement columns, current usage, derived over-limit booleans,
media policy, and experience entitlements — is achievable and is the right target. Two constraints
from the existing design must be respected:

* **Keep the materialised enforcement columns.** The comment at `app.py:2598-2608` is correct:
  computing the limit dynamically inside the reservation would force a read-then-write and lose the
  atomic guarantee. The resolver should *feed* those columns, not replace them.
* **Keep feature eligibility separate from service coverage.** `project_public_access_state()` must
  stay independent (Section 8).

### User entitlement visibility (current state)

`templates/user/profile.html` shows plan name, plan duration, projects used vs limit, scans used vs
limit, and — via `project_capacity_summary` — base slots, effective slots, used, and remaining
(`:443-521`, `:618-705`). Missing entirely: **storage** (any of it), **max pairs per project**,
**media limits** (image size, video size, duration, dimensions), **experience-mode entitlements**,
and any **account-state explanation** for over-limit conditions. The template also carries its own
`999999` sentinel, disagreeing with both the model and the resolver.

---

## 12. Admin / Super Admin UI Gap Matrix

Surface today: **69 `/admin*` routes** — 52 with `require_admin_permission`, 9 with bare
`@admin_required`, 8 with no decorator (5 intentional auth/entry routes + 3 public media routes).
No admin route is `@csrf.exempt`, and every state-changing admin form and `fetch()` POST carries a
token.

### 12.1 Plan configuration fields (locked target)

| Field | Backend exists? | Current UI? | Recommended UI | Access | Decision |
| --- | --- | --- | --- | --- | --- |
| Plan name | yes | yes (`add_plan.html:509`) | keep | superadmin | `EXISTS` |
| Plan family (Individual / Business-Vendor) | **no column** | no | select | superadmin | `SCHEMA_REQUIRED` → `ADD` |
| Price / offer price / currency | yes | yes (`:534`, `:541`, `:516`) | keep | superadmin | `EXISTS` |
| Billing / validity period | yes | yes (`:550`, `:560`) | keep + fix the `*30` month math | superadmin | `PARTIAL` → `IMPROVE` |
| Project capacity | yes | yes (`:569`) | keep | superadmin | `EXISTS` |
| Scan allowance | yes | yes (`:584`) | keep | superadmin | `EXISTS` |
| Max pairs per project | yes | yes (`:576`) | keep + live-effect warning | superadmin | `PARTIAL` → `IMPROVE` |
| Max image size | **no column** | no | number, bounded by server ceiling | superadmin | `SCHEMA_REQUIRED` → `ADD` |
| Max video size | **no column** | no | number, bounded by server ceiling | superadmin | `SCHEMA_REQUIRED` → `ADD` |
| Max video duration | **no column** (env exists, disabled) | no | number | superadmin | `SCHEMA_REQUIRED` → `ADD` |
| Max image dimensions / pixels | **no column** (env exists) | no | number | superadmin | `SCHEMA_REQUIRED` → `ADD` |
| Base total account storage | **no column, no concept** | no | number | superadmin | `SCHEMA_REQUIRED` → `ADD` |
| Experience: Direct QR enabled | no | no | checkbox | superadmin | `SCHEMA_REQUIRED` → `ADD` |
| Experience: Detect Once enabled | no | no | checkbox | superadmin | `SCHEMA_REQUIRED` → `ADD` |
| Experience: Tracked Overlay enabled | no | no | checkbox | superadmin | `SCHEMA_REQUIRED` → `ADD` |
| Vendor / business capabilities | partial (user-level `account_type` only) | no | checkbox group | superadmin | `SCHEMA_REQUIRED` → `ADD` |
| Availability / publish state | `is_active` boolean | yes (`:621`) | lifecycle select | superadmin | `PARTIAL` → `IMPROVE` |
| Plan version / revision | **no** | no | read-only version + "duplicate plan" action | superadmin | `SCHEMA_REQUIRED` → `ADD` |
| Absolute server ceilings | env constants | no | **read-only display** beside each plan limit | superadmin | `SERVER-ONLY` → `READ-ONLY` |
| Impact preview before save | no | no | "N subscribers affected; M projects exceed the new pair limit" | superadmin | `ADD` |
| Scanner thresholds / ORB / homography / RANSAC / optical flow / calibration | n/a | none | — | — | `DO-NOT-EXPOSE` |

**The dangerous asymmetry that must be resolved alongside these additions:** project and scan limits
are *snapshotted* onto the User row at activation (`app.py:8636-8637`), so editing them does not
touch existing subscribers — but `max_pairs_per_project` is read **live** on every request
(`app.py:2986`), so lowering it instantly reduces every existing subscriber's allowance mid-term.
The Admin UI gives no indication that two adjacent fields on the same form behave in opposite ways.
Whichever convention V1.1 adopts must be uniform, and the new media/storage fields must follow it
deliberately rather than by accident.

### 12.2 Production operations capabilities

| Capability | Backend exists? | Current UI? | Recommended UI | Access | Reason | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| App health / version / commit | `/healthz` only | no | health tile on `/admin/operations` | superadmin | first question in any incident | `ADD` |
| DB reachable | `/ready` | no | health tile | superadmin | | `ADD` |
| Redis reachable | `_rq_diagnostics_payload` `app.py:12858-12891` | **yes** (`operations.html`) | keep | superadmin | | `EXISTS` |
| Queue depth / running / failed | yes, same payload | **yes** | keep | superadmin | | `EXISTS` |
| **Worker count online** | no (`rq.Worker.count` never called) | no | add to the RQ panel | superadmin | Redis up ≠ worker consuming | `ADD` |
| **Oldest waiting job age** | data exists (`ProcessingJob.queued_at`) | no | add to the RQ panel | superadmin | the actual backlog signal | `ADD` |
| Retry a failed job | `retry_failed_job` imported `app.py:77`, **never called** | no | audited button | superadmin | backend already written | `ADD` |
| Media directory writable | no | no | health tile | superadmin | silent upload failure mode | `ADD` |
| Migration revision vs head | Flask-Migrate wired; no route | no | read-only tile | superadmin | deploy-correctness signal | `ADD` (`READ-ONLY`) |
| Effective hard server ceilings | env constants | no | read-only panel | superadmin | needed to interpret plan limits | `READ-ONLY` |
| Per-plan upload policy | pending schema | no | plan editor (12.1) | superadmin | | `ADD` |
| **Account storage usage** | **nothing** | no | per-account + largest-accounts view | superadmin | core V1.1 commercial need | `ADD` (blocked on Section 9) |
| Total / video / image / QR media usage | no | no | summary tiles | superadmin | | `ADD` |
| Temp + chunk usage | data exists (`UploadSession`) | partial (last 25 sessions) | add byte totals | superadmin | | `IMPROVE` |
| Disk free capacity | no | no | tile only if the host can expose it safely | superadmin | | `SERVER-ONLY` → `DEFER` |
| Over-storage accounts | no | no | list | superadmin | | `ADD` (blocked on Section 9) |
| Upload diagnostics (recent failures) | last 25 `UploadSession` rows `app.py:12903-12908` | **yes** | group by failure reason; `safe_basename` already prevents path leakage | superadmin | | `IMPROVE` |
| SMTP configured / host / port / TLS | `_smtp_diagnostics_payload` `app.py:12832-12856` | **yes** | keep | superadmin | | `EXISTS` |
| SMTP last success / failure | **not tracked** | no | add once tracked | superadmin | | `ADD` |
| Safe test email | no | no | — | superadmin | mail-bomb / spoof vector without careful design | `DEFER` |
| SMTP password / DB password / Razorpay secret / session secret / raw tokens / password hashes | — | none | — | — | | `DO-NOT-EXPOSE` |
| Failed payments | `/admin/payments` | **yes** | keep | admin | | `EXISTS` |
| Webhook processing state | `/admin/webhook-events` `app.py:12950` (metadata only) | **yes** | keep | admin | | `EXISTS` |
| Refund provider state | `/admin/api/refunds/<id>` | detail only | keep | superadmin | | `EXISTS` |
| **Refunds needing manual review** | `MANUAL_REVIEW_REQUIRED` is written and never listed | **no** | queue view | superadmin | money returned, service still active (P1-29) | `ADD` |
| Moderation queue | `/admin/moderation` + 3 JSON routes | **yes** | keep | admin | | `EXISTS` |
| Abuse / rate-limit metrics | in-memory, per worker | no | — | | meaningless until the limiter is shared | `DEFER` |
| Backup status / age / last restore rehearsal | **nothing** | no | read-only, only with machine-readable host evidence | superadmin | | `SERVER-ONLY` → `DEFER` |
| Central limiter enabled? | `RATE_LIMIT_REDIS_URL` reserved, never read | no | read-only security tile | superadmin | | `ADD` (after P0-8) |
| Add-on catalogue CRUD | **no seed, no route** | no | full CRUD | superadmin | P0-3 — product is dark without it | `ADD` |
| Ownership transfer / claim administration | helpers exist, **zero routes** | no | queue + admin override | superadmin | locked V1.1 requirement | `ADD` |
| `.env` editor / SQL console / shell / Redis console / filesystem browser / Nginx / Gunicorn / firewall editing | — | **none present** | — | — | verified absent: no `subprocess`, `os.system`, `eval`, `exec`, `os.listdir`, or request-reachable `text()` SQL in `app.py` | `DO-NOT-EXPOSE` (already compliant) |
| One-click destructive DB restore | — | none | — | — | | `DO-NOT-EXPOSE` |

### 12.3 Admin authorization and audit defects found

| ID | Finding | Evidence | Decision |
| --- | --- | --- | --- |
| A-01 | `/dashboard?admin_view=true&user_id=N` runs with **no permission check at all** — `login_required` contains an explicit carve-out testing only `current_admin()` truthiness and `view.__name__ == "dashboard"` | `app.py:2466-2475`, `5111-5121` | `P1-HARDENING` — gate on `admin.users.view`. No privilege gain today (role `admin` already holds that permission), but it defeats any future tightening |
| A-02 | `admin_delete_own_project` deletes files and rows with **no `AdminActivity` record**, unlike its fully audited superadmin twin | `app.py:13419-13432` vs `12399-12446` | `P1-HARDENING` |
| A-03 | Plain `admin` can grant/revoke paid entitlements that the subscriptions module reserves for superadmin: `add-scans` (`11466`), `grant-extra` (`12708`), `update-limit` (`12679`, sets an **arbitrary** value), `extend-trial` (`11434`), `lock-scanner` (`12730`) | as cited | `P1-HARDENING` — the superadmin gate on `/admin/subscriptions/<id>/increase-limits` provides no real control while these exist |
| A-04 | All 9 admin JSON endpoints return an **HTML 302** on auth failure; `fetch()` follows it and `response.json()` throws a parse error instead of surfacing "session expired". The global JSON branch keys on `/api`, and these paths start `/admin/api` | `app.py:2228-2236`, `13452` | `P1-HARDENING` — one fix in the decorator |
| A-05 | Same class on CSRF failure: three admin JSON POSTs send no `Accept: application/json`, so a stale token returns an HTML 400 body to a JSON parser | `app.py:210-237`; `view_payment.html:1009`, `operations.html:338`, `moderation.html:263` | `P1-HARDENING` |
| A-06 | `serve_admin_video` lacks the `video_filename` null-check its sibling `serve_admin_image` has → unhandled 500 | `app.py:13333-13348` vs `13324` | `P2-POST-GO-LIVE` |
| A-07 | Dead authz surface invites a future bypass: `Admin.permissions_json` defaults to granting everything including `manage_admins` and is never read; `super_admin_required` is defined and applied to zero routes | `models.py:783-786`; `app.py:2523-2524` | `P1-HARDENING` — delete or wire deliberately |
| A-08 | Three unauthenticated `/admin/image|video|qr` routes serve admin-owned project media to anyone who guesses `project_id`/`pair_index`, while emitting `Cache-Control: private` | `app.py:13310`, `13333`, `13350` | `P1-HARDENING` — by design for the scanner path; record as accepted risk and fix the misleading header |
| A-09 | Three inert forms in `settings.html` (`#generalForm`, `#paymentForm`, `#securityForm`) have no action and no CSRF token; the route deliberately processes only three free-trial keys | `settings.html:623,720,758`; `app.py:12756-12777` | `P2-POST-GO-LIVE` — remove or label |

**What is already right and must not regress:** `current_admin()` re-reads the `Admin` row from the
database on every request and re-validates `is_active` and role, so deactivating an admin takes
effect immediately; the last-active-superadmin invariant is enforced with `with_for_update()`
(`app.py:2192-2211`); self-deletion and self-deactivation are blocked (`11103`, `11134`); plan
deletion is blocked while any user references the plan (`11776`); every high-impact permission
denial is audit-logged (`app.py:2231-2233`); `_safe_basename` strips paths before display; and
diagnostics expose booleans (`redis_configured`, `host_configured`) rather than values.

---

## 13. Recommended Admin Production Information Architecture

Evidence-backed only. Each item maps to a gap identified above.

**Keep as-is** (already good): `/admin/dashboard`, `/admin/users*`, `/admin/projects*`,
`/admin/scans*`, `/admin/payments*`, `/admin/webhook-events`, `/admin/moderation`,
`/admin/capacity`, `/admin/activity-logs`.

**Extend `/admin/operations` into a real operations console** (superadmin, read-only except where
noted). It already carries seven panels and is the natural home:

1. **Platform health tile** — app version/commit, DB, Redis, **worker count**, media-directory
   writable, **Alembic current vs head**. Closes P1-07 and gives the deploy check a UI.
2. **Queue panel** (extend) — worker count, oldest waiting job age, and a superadmin-only audited
   **Retry failed job** button using the already-imported `retry_failed_job`.
3. **Storage panel** — total media bytes split by image/video/QR/features/temp, largest accounts,
   largest projects, over-storage accounts. **Blocked on Section 9.**
4. **Upload diagnostics** (extend) — group the existing last-25 sessions by failure code; add
   temp/chunk byte totals.
5. **Effective limits panel (read-only)** — the absolute server ceilings (`MAX_IMAGE_SIZE`,
   `MAX_VIDEO_SIZE`, `MAX_IMAGE_PIXELS`, `MAX_CONTENT_LENGTH`, resumable chunk cap) displayed beside
   the configured plan limits, so an admin can see that a plan value is bounded by a server value
   they cannot change. This is the concrete implementation of the "absolute server safety ceiling"
   requirement.
6. **SMTP panel** (extend) — last success / last failure once tracked. No test-send in V1.1.

**New superadmin pages:**

7. **`/admin/addons`** — full `AddonCatalog` CRUD. Without it the add-on product cannot exist in
   production (P0-3).
8. **`/admin/refunds`** — a refund queue filtered on `reconciliation_status`, defaulting to
   `MANUAL_REVIEW_REQUIRED` (P1-29). Those rows are written today and never listed.
9. **`/admin/transfers`** — ownership transfer and claim administration, including
   `PENDING_CAPACITY` resolution. Required by the locked transfer rules; there is currently no HTTP
   surface at all (Section 17).

**Reworked:**

10. **Plan editor** — the Tier-2 fields from 12.1, a lifecycle state, a **duplicate plan** action,
    and an **impact preview** on save (subscriber count; projects that would exceed a new pair
    limit; media that would exceed a new size limit). The preview is what makes *"Admin changing
    500MB→100MB must not delete existing 400MB media"* verifiable rather than merely true by
    accident.
11. **User detail** — replace the single combined counter with an explicit breakdown: plan base +
    purchased add-ons + admin grants + current usage = effective entitlement, per dimension.
    Unachievable until admin grants write ledger rows (Section 11).

**Explicitly not recommended now:** disk-free display, safe test email, backup status, and abuse
metrics — all `DEFER` until the host exposes reliable machine-readable evidence (backup, disk) or
the limiter is shared (abuse). Everything on the `DO-NOT-EXPOSE` list is already absent and must
stay absent.

---

## 14. Upload / Media Limits Audit

### Classification of every limit

| Setting | Value | Where | Classification |
| --- | --- | --- | --- |
| `MAX_IMAGE_SIZE` | 50 MiB | `app.py:3555`, env `MAX_IMAGE_UPLOAD_BYTES` | `SERVER-ONLY`, `RESTART-REQUIRED` → must become `PLAN-CONFIGURABLE` under a server ceiling |
| `MAX_VIDEO_SIZE` | 1 GiB | `app.py:3556`, env `MAX_VIDEO_UPLOAD_BYTES` | same |
| `MAX_IMAGE_DIMENSION_PX` | 8000 | `app.py:3557` | same |
| `MAX_IMAGE_PIXELS` | 40,000,000 | `app.py:3558` | same |
| `MAX_VIDEO_DURATION_SECONDS` | **`0` → `None` → check disabled** | `app.py:3560` | `MISSING` in practice → must become `PLAN-CONFIGURABLE` |
| `MAX_CONTENT_LENGTH` | **unset** | `app.py:3566-3568` | `MISSING` → `SERVER-ONLY` + `SERVER-TEAM-VERIFY` (P0-7) |
| Resumable chunk cap | 1 MiB | `app.py:6632`, env | `SERVER-ONLY`, `RESTART-REQUIRED`; proxy must match (`docs/production/README.md:76,94`) |
| Allowed image formats | JPEG + PNG by magic bytes | `upload_validation.py:33-37` | `SERVER-ONLY` — correctly not configurable |
| Allowed video format | ISO-BMFF `ftyp`, stored `.mp4` | `upload_validation.py:40-42` | `SERVER-ONLY` |
| `max_pairs_per_project` | 10 (plan default) | `models.py:35` | `PLAN-CONFIGURABLE`, `DYNAMIC` — already correct |
| Upload rate limit | 8 starts / 3600 s / IP | `app.py:280` | `SERVER-ONLY`, process-local (P0-8) |
| Upload session TTL | 1440 min | `app.py:6629` | `SERVER-ONLY` |
| Session stale / cleanup batch | 120 min / 200 | `app.py:6630-6631` | `SERVER-ONLY` |

**None of the media limits is plan-aware, admin-aware, or dynamic.** All are module-level constants
read once at import, so any change requires a process restart. The creator UI is **not**
synchronised: `templates/user/user_create_project.html:3372,3898` hard-codes 50 MB / 1 GB / 1 MB as
JS literals, while `MAX_PAIRS_PER_PROJECT` on line 2100 of the same file is correctly
server-injected — proving the injection pattern already exists and simply was not applied to the
media limits (P1-18).

### Enforcement points

All delegate to `upload_validation.py`: `app.py:5575-5595` (pair replacement), `6231-6240`
(multipart create), `7220-7235` (resumable finalize), `13085-13094` (admin create), plus declared
size prechecks at `6912-6920` and the chunk body cap at `7013-7018`.

A parallel, unused limit set exists in `media_processing.py:30` (20 MB image, 64 px minimum) and
`:92` (codec allowlist) belonging to the disabled Experience Creator pipeline. It does not gate the
live path — a documented deliberate split (`upload_validation.py:3-5`) — but it is a second source
of truth waiting to confuse a future maintainer.

### Validation quality — genuinely strong

* **Magic bytes are the primary gate**, with a decoder cross-check: Pillow's `img.format` must equal
  the signature-detected format (`upload_validation.py:116-119`), defeating polyglot prefixes. Video
  is not merely container-inspected — a real `cv2.VideoCapture` open **and a decoded frame read**
  are required (`:185-194`).
* **MIME is never trusted.** `FileStorage.mimetype` is stored for display only; extensions are
  assigned by the server from the detected format.
* **Filename sanitisation and traversal: solid.** Client filenames never touch the filesystem —
  `tempfile.mkstemp` with a server prefix (`upload_validation.py:61`); final names are
  `{project_id}_{index}.{ext}` (`app.py:6297-6298`, `7315-7316`, `13138-13139`); resumable temp
  paths derive only from a server UUID4, with a defence-in-depth root check before any delete
  (`app.py:6691-6702`); `storage.py:20-29` rejects NUL, `..`, absolute paths and drive letters. One
  gap: the multipart path stores `image_file.filename` raw into a `String(255)` column
  (`app.py:6341-6342`) — never used as a path, but unsanitised and unbounded, where the resumable
  path correctly uses `_sanitize_display_text`.
* **Zero-byte and malformed media** are both rejected (`upload_validation.py:81-82`, `171-172`,
  `102-106`, `187-194`).
* **Image bombs: covered by the app rather than by Pillow.** `PIL.Image.MAX_IMAGE_PIXELS` is never
  set (so Pillow's ~89 MP default applies), but the app's own 40 MP check runs **before**
  `img.load()` (`upload_validation.py:133-138`) — the load-bearing ordering. The
  `LOAD_TRUNCATED_IMAGES` save/restore around validation (`:112-113,144`) correctly counteracts the
  global `True` at `app.py:262`, but is process-global and not thread-safe under the
  `ThreadPoolExecutor` (P1-21).
* **Validation order:** size is checked after the file is fully spooled (`upload_validation.py:77` →
  `80-86`). Unavoidable for multipart, which is exactly why the proxy body cap matters (P0-7).

### Resumable upload — the strongest subsystem in the codebase

Row-locked chunk critical section (`app.py:6705-6713`); crash self-heal that truncates back to
`current_offset` (`:7044-7047`) or fails closed with `STORAGE_INCONSISTENT` (`:7048-7053`);
idempotent duplicate-chunk no-op (`:7086-7107`); `current_offset` re-read from `os.path.getsize`
rather than arithmetic (`:7066`); DB `CheckConstraint`s backing every invariant
(`models.py:1996-1998`); atomic conditional-UPDATE finalize gate (`:7552-7556`) with a separate
retry-from-`assembled` path (`:7506-7512`); optional client checksum verified before any quota or
Project work (`:7188-7198`); and `_BoundedFileView` (`:6797-6819`) validating slices of the assembled
file without a second on-disk copy. Quota is advisory at session create and authoritative at
finalize — the same point as the multipart path. Preserve intact.

### Cleanup

`flask cleanup-upload-sessions` (`app.py:7608-7647`) is correct — dry-run by default, batch-bounded,
never touches `completed` sessions — and is **never scheduled** (P1-22). Abandoned `.part` files
accumulate indefinitely.

---

## 15. Security / Abuse / Rate-Limit Audit

### Rate-limiting mechanism: `PROCESS-LOCAL`

`rate_limit.py:13-63` — a fixed-window `collections.deque` in a `defaultdict`, guarded by a
`threading.Lock`, one module-global instance (`:63`). Flask-Limiter is **not installed**. Redis is a
dependency but is used only by RQ. `RATE_LIMIT_REDIS_URL` is documented
(`docs/production/README.md:77`) and read by no code.

Under `gunicorn -w N` every published limit is effectively `×N`, and a rolling restart clears all
counters (`app.py:80` also calls `request_limiter.clear()` on import). This is documented as a known
gap (`docs/production/README.md:102,122-123`; `security-proxy-checklist.md:19-23`) and was accepted
for single-process V1 — it is **not** acceptable for a multi-worker V1.1.

| Endpoint | Mechanism | Limit |
| --- | --- | --- |
| `/login/` | `PROCESS-LOCAL` + `DATABASE` lockout | 80/900 s IP; 4 failures/3 h per user |
| `/register` | `PROCESS-LOCAL` | 30/3600 s IP |
| `/forgot-password` | `PROCESS-LOCAL` | 30/3600 s IP |
| `/resend-otp` | `PROCESS-LOCAL` + `DATABASE` | 20/3600 s IP; 60 s interval, 3/900 s |
| OTP verification | `DATABASE` | 5 attempts → 15 min lock (`app.py:1333-1336`) |
| `/upload` | `PROCESS-LOCAL` | 8/3600 s IP+user |
| Content report | `PROCESS-LOCAL` | 5/3600 s IP+project+session |
| Scanner init / track / session-end / fallback / telemetry | `PROCESS-LOCAL` | 45 / 240 / 90 / 60 / 30 per 60 s |
| **`/admin/login`** | `DATABASE` only (per-email) | **`NONE` per IP** (P0-8) |
| **`/admin/forgot-password`** | **`NONE`** | unlimited OTP mail (P0-8) |
| `/admin/reset-password`, `/verify-email/`, `/reset-password/` | `DATABASE` (OTP internals) only | |
| `/create-razorpay-order`, `/verify-payment`, `/api/addons/orders`, `/api/addons/purchases/<id>/verify` | `NONE` | P1-15 |
| All four `/api/uploads/sessions*` routes | `NONE` | P1-15 — the resumable path sits outside the `upload` limiter |
| `/send-contact-email` | `NONE` (reCAPTCHA only, which fails open) | P1-10, P1-15 |
| `/webhooks/razorpay` | `NONE` — **deliberate and correct** | HMAC + DB unique-index idempotency; documented at `app.py:9073-9078` and `security-proxy-checklist.md:67-71` |

Key derivation is correct: `_client_ip()` (`app.py:1220-1224`) uses `request.remote_addr` after
`ProxyFix(x_for=1, x_proto=1, x_host=1)` (`app.py:92`) and never reads `X-Forwarded-For` directly.
This depends on exactly one trusted proxy hop — `SERVER-TEAM-VERIFY`.

### CSRF

`PASS`. Global `CSRFProtect` (`app.py:207`) with `WTF_CSRF_CHECK_DEFAULT=True` and header names
configured (`:109-111`). Every `method="POST"` form in `templates/` carries `csrf_token` — verified
with no exceptions apart from the three inert placeholder forms in `settings.html` (A-09). Exactly
seven `@csrf.exempt` routes, **none of them admin**:

| Route | Verdict |
| --- | --- |
| `POST /webhooks/razorpay` (`app.py:9066`) | Correct and exemplary — HMAC over raw bytes before parsing, fails closed on a missing secret |
| `POST /detect_init`, `/detect_track`, `/api/scanner/session/end`, `/api/scanner/<id>/fallback-event`, `/api/scanner/<id>/opencv-telemetry` | Correct — public scanner; `sendBeacon` cannot set headers |
| `POST /api/projects/<id>/report` (`app.py:8480`) | Acceptable with a caveat — the route is public, but `current_user()` is consulted (`:8509`) and the reporter is attributed (`:8512-8513`), so a logged-in user could be CSRF'd into filing an attributed report. Impact is low (human review only) |

The resumable-upload API is correctly **not** exempt and relies on `X-CSRFToken`.

### Other security posture

| Item | Verdict |
| --- | --- |
| Secrets | `PASS` — zero hard-coded credentials; no `.env` tracked; `.gitignore` covers `.env*`, `*.pem`, `*.key`, `credentials.json`, `*.db`, `backups/`, `logs/`, `data/`. `FLASK_SECRET_KEY` has no fallback and fails startup. Reported by file/type only; no secret value was read or reproduced |
| Historical backdoor | `PASS` — `add_simple_admin.py:1-15` documents the removal of a committed `admin@gmail.com`/`admin123` default; it now requires three env vars and refuses on collision |
| Open redirect | `PASS` — zero `next`/`return_url` parameters; every `redirect()` takes a literal `url_for`; `form-action 'self'`, `base-uri 'self'` |
| Debug mode | `PASS` — `app.run` only under `__main__`, `debug=FLASK_DEBUG_ENABLED`, forced off under testing |
| Windows / localhost paths in app code | `PASS` — zero `C:\`/`F:\` in `app.py`, `models.py`, `core/*.py`; all paths derive from `BASE_DIR`. One hard-coded host set in the reCAPTCHA hostname allowlist (`app.py:455`), scoped and low-risk |
| Dev/test hooks | `PASS` — no dev routes exist. Dev-test-user machinery is CLI-only, triple-gated (`FLASK_ENV=development` **and** `SCANSTORY_DEV_TESTING=1` **and** no production flag, `app.py:3052-3057`), further restricted to a hard-coded 10-email allowlist plus a DB identity check, and startup **refuses to boot** if a production flag coexists with `SCANSTORY_DEV_TESTING` (`app.py:143-144`). Exemplary — `SCANSTORY_TESTING` should join that prohibition (P0-6) |
| Security headers | `PARTIAL` — nosniff, `Referrer-Policy`, `X-Frame-Options: SAMEORIGIN`, `Permissions-Policy: camera=(self)`; CSP report-only by default with `'unsafe-inline'` on script and style (P2-08); HSTS off by default |
| Error pages | `PASS` globally (`app.py:13438-13466` never returns `str(error)`), **but** five route-local handlers bypass it (P1-01, P1-02) |
| PII in logs | `PARTIAL` — sanitisers are real and allowlist-based (`processing_queue.py:87-92`; `app.py:333-334,360-366,388-392`), Razorpay logs are ID-only, the bootstrap password is never logged; but `_otp_log` writes user emails at INFO (`app.py:1227-1235`) under an unconditional DEBUG root level with no rotation |

---

## 16. Authentication / Authorization Audit

| Area | Verdict | Evidence |
| --- | --- | --- |
| Password hashing | `PASS` | `werkzeug.security.generate_password_hash` default (scrypt on Werkzeug ≤2.3.8) |
| Password policy | `PARTIAL` | user minimum 6 (`app.py:5719`, `6078`) vs admin reset 8 (`:10939`); no complexity or breach check (P2-09) |
| OTP generation | `PASS` | `secrets.randbelow(1000000)` (`app.py:1207-1208`), stored **hashed**; legacy plaintext compared with `secrets.compare_digest` |
| OTP binding | `PASS` | `challenge_id = secrets.token_urlsafe(24)` bound into the session (`app.py:1280`, `5793`, `6044`, `10909`) — an OTP is only redeemable on the requesting device |
| OTP expiry / single use | `PASS` | `expires_at` (`:1287`); atomic conditional-UPDATE claim (`:1342-1353`); reissue invalidates prior codes (`:1276-1278`) |
| OTP brute force | `PASS` | 5 attempts → 900 s lock (`:1333-1336`); resend 60 s interval, 3/900 s, plus IP caps (`:1372-1382`) |
| Email verification enforcement | `PASS` | re-checked on every request by `login_required` (`app.py:2483-2488`) |
| Session fixation | `PARTIAL` | no `session.clear()` on login for users (`app.py:6011`) or admins (`:2492-2496`) — P1-12 |
| Session lifetime | `MISSING` | `PERMANENT_SESSION_LIFETIME` never set, `session.permanent` never used — no expiry and no idle timeout exist (P1-11) |
| Cookie flags | `PASS` | HttpOnly + SameSite=Lax always; Secure env-driven and **required** in production (`app.py:141-142`) |
| User brute-force / lockout | `MISCONFIGURED` | keyed on `user_id` only and never cleared on success → anonymous account-lockout DoS, and persistent lockout after a successful login (P1-08) |
| Admin lockout | `PASS` | 5 failures → 15 min, persisted in `SystemConfig` so it survives restart and is shared across workers; generic error for every failure mode; correctly cleared on success (`app.py:1521-1557`, `10883`) |
| Enumeration | `PARTIAL` | `/forgot-password` is identical in all branches (good); `/register` reveals existing emails (P2-10) |
| Blocked / inactive enforcement | `PASS` (design) / `PARTIAL` (coverage) | `current_user()` and `current_admin()` re-read the row from the DB every request, so blocking takes effect on the next request with no session store to purge — the right design. Four resumable-upload routes are not covered (P1-09) |
| IDOR | `PASS` | One shared `user_can_manage_project()` (`app.py:1771-1777`) applied at `5513`, `5529`, `5549`, `5561`, `5662`, `7667`, `7712`, `8458`, `9417`, `9618`, `10828`; upload sessions via `_upload_session_owned()` at all four mutation points; payment ownership at `8826-8828`. Returns 404, never 403 |
| Admin RBAC model | `PASS` | `VALID_ADMIN_ROLES` + `ADMIN_ROLE_PERMISSIONS` (`app.py:1476-1508`); `admin_has_permission` checks `is_active`, normalises the role, rejects unknown roles |
| Admin route coverage | `PARTIAL` | 52 of 69 permission-gated; 9 bare `@admin_required` (all but three ownership-scoped); the `login_required` carve-out is the one route with no permission check at all (A-01) |
| High-impact permissions | `PASS` | denials audit-logged (`app.py:2231-2233`); last-superadmin protected with `with_for_update()` |
| Permission model integrity | `PARTIAL` | phantom `permissions_json` (A-07) and superadmin/plain-admin capability equivalence on entitlement grants (A-03) |
| JSON auth failures | `MISCONFIGURED` | all 9 admin JSON endpoints return HTML redirects (A-04, A-05) |

---

## 17. Ownership / Vendor / Transfer Audit

### Schema — complete and well designed

`Project` carries all six ownership columns (`models.py:866-871`): `owner_user_id`,
`owner_admin_id`, `created_by_user_id`, `current_owner_user_id`, `manager_vendor_user_id`,
`beneficiary_user_id`. Supporting tables: `ProjectOwnershipTransfer` (`models.py:930-963`) with
statuses `PENDING_ACCEPTANCE`, `PENDING_CAPACITY`, `COMPLETED`, `CANCELLED`, `EXPIRED`, `DISPUTED`;
`ProjectOwnershipClaim` (`:966-996`) with nine statuses including `PENDING_ADMIN_REVIEW`;
`ProjectServiceCoverage` (`:999-1032`). Migration `d2a4b6c8e0f1` ships all of it.

Resolution helpers are consistent and correct:

* `project_current_owner_user_id()` = `current_owner_user_id or owner_user_id` (`app.py:1763`)
* `user_can_manage_project()` = current owner **or** `manager_vendor_user_id` where the user is a
  `BUSINESS_VENDOR` (`app.py:1771-1777`)
* `project_user_access_filter()` (`app.py:1784-1789`) covers all three list cases
* `set_project_current_owner()` (`app.py:1799-1808`) sets **both** `current_owner_user_id` and
  `owner_user_id`, keeping the legacy column and the ORM cascade in sync — this is deliberate and
  prevents a whole class of divergence bug that a reviewer might otherwise suspect.

### The finding: the entire subsystem has no HTTP surface

`grep` for `@app.route` matching `transfer`, `claim`, or `ownership` returns **zero routes**. The
call graph is:

```
set_project_current_owner              <- called only from accept_project_ownership_transfer
initiate_project_ownership_transfer    <- called only from approve_project_ownership_claim_by_admin
accept_project_ownership_transfer      <- NO CALLER
create_project_ownership_claim         <- NO CALLER
approve_project_ownership_claim_by_admin <- NO CALLER
```

Tables, states, indexes, migration, and helpers all ship; the product surface does not. The only
exercise is in `tests/integration/test_domain_ownership_foundation.py` and
`test_domain_commercial_capacity_and_reporting.py`. **`PENDING_CAPACITY` cannot be entered or
exited by any user or admin.** The sibling agent's V1.1 UX work
(`tests/gate_jr/test_v11_commercial_ownership_ux.py`) covers vendor *identity and display*, not the
transfer *workflow*.

Classification: `MISSING` (surface), not `BLOCKER` (nothing is broken — the feature is simply not
reachable). But it directly blocks the locked V1.1 transfer requirements and must be part of the
implementation plan.

### Behavioural review of the (currently unreachable) transfer logic

| Locked rule | Verdict | Evidence |
| --- | --- | --- |
| Storage charged to the current owner | `N/A` | no storage concept |
| Transfer accounts for recipient project capacity | `PASS` | `_reserve_project_quota_atomic(recipient)`, `PENDING_CAPACITY` on failure (`app.py:1853-1856`) |
| Transfer accounts for recipient storage capacity | `MISSING` | no check possible |
| If the recipient lacks capacity the transfer stays safe and non-destructive | `PASS` | parks in `PENDING_CAPACITY`, project untouched |
| No project or media disappears on a failed transfer | `PASS` | `except Exception: db.session.rollback(); raise` (`app.py:1874-1876`) |
| Ownership rules enforced | `PASS` | wrong-owner guard (`1850-1851`), recipient-only acceptance (`1842-1843`), single-active-transfer guard (`1819-1820`) |

Two defects to fix when the surface is built:

* `sender.projects_used = max(0, … - 1)` (`app.py:1859`) is a Python read-modify-write, not the
  atomic conditional UPDATE used everywhere else — a concurrency inconsistency.
* The function only `flush()`es; the commit boundary belongs to a caller that does not exist. Once
  routes are added, transaction ownership must be defined explicitly.
* Transfer does not check the recipient's **subscription status**, only capacity. Because
  `project_public_access_state` reads the *new* owner's subscription (`app.py:2110-2114`),
  transferring to an expired account takes the QR dark immediately unless project-specific coverage
  exists. `TRANSFER_CARRY_OVER` exists as a coverage source precisely for this; wiring it is a
  product decision.

### Coverage after transfer

`_project_specific_coverage_candidates` (`app.py:1929-1937`) filters by `project_id` only and never
re-derives the owner, so coverage created under a previous owner keeps a project live for a new
owner — for **all** coverage sources, not only `TRANSFER_CARRY_OVER`. Whether this is intended needs
an explicit product decision (Section 30, ANM-19).

### Dead field

`beneficiary_user_id` is declared with a relationship (`models.py:871,915`) and is **never read or
written anywhere** in `app.py`. Either give it meaning in the V1.1 vendor model or drop it.

### Individual ↔ Business/Vendor

Covered in Section 8. No conversion flow exists in either direction and no guard exists.
Recommended posture (do not implement yet): Individual → Vendor is a safe additive activation;
Vendor → Individual must be blocked while any managed project, active transfer, or open claim
exists, because `user_can_manage_project` requires `is_business_vendor(user)` (`app.py:1777`) and a
conversion would strand every project whose only manager is the converting account.

---

## 18. Subscription / Capacity / Coverage Audit

### Subscription representation

There is **no `Subscription` table.** State is denormalised onto `User` (`models.py:155-171`):
`subscription_id`, `subscription_taken_at`, `subscription_expires_at`, `subscription_status`
(`trial` / `active` / `expired` / `limit_reached`), `subscribed_project_limit`,
`subscribed_scan_limit`, `projects_used`, `scans_used`. `PaymentOrder` snapshots
`purchased_project_limit` / `purchased_scan_limit` at purchase time (`models.py:425-426`), which is
the only historical record of what a customer actually bought — valuable, and the natural basis for
plan versioning.

### Capacity mechanics

| Mechanism | Verdict | Evidence |
| --- | --- | --- |
| Project quota consume | `PASS` | `_atomic_increment_user_counter` (`app.py:2575-2592`): one conditional UPDATE, `or_(limit IS NULL, limit == 0, coalesce(counter,0) < limit)` — no check-then-write |
| Scan quota consume | `PASS` | same helper, plus an upstream `ScanLog.counted` conditional-UPDATE claim (`app.py:10430-10438`) so a session counts at most once |
| Purchased project capacity | `PASS` | `EntitlementTransaction` ledger + `reconciled_project_limit()`; unique constraint `uq_entitlement_source_type_id_type` makes fulfilment idempotent at the DB level |
| Purchased scan capacity | `FAIL` | destroyed on the next activation (P0-1) |
| Project quota release on delete | `PASS` | decremented at `app.py:5535` (user delete, via `project_current_owner_user_id`) and `12413` (admin delete). `admin_delete_own_project` (`13419`) correctly does not decrement — admins have no quota |
| Launch capacity gate | `PASS` | `_reserve_capacity_slot_atomic` (`app.py:2732-2773`) is a single conditional UPDATE; release is conditional-on-`reserved` and idempotent; `CapacityConfig` docstring documents the invariant precisely (`models.py:445-457`) |
| Reservation TTL expiry | `PARTIAL` | `expire-stale-reservations` CLI exists (`app.py:3289`) and is never scheduled — abandoned checkouts hold slots (P1-22) |
| Counter drift repair | `PARTIAL` | `reconcile-quota-counters` (`app.py:3239-3262`) exists and is never scheduled; its very existence documents that drift occurs (P0-1) |

`CapacityConfig` is a **paid-account admission gate** (default limit 25), not a concurrency control.
It must not be confused with concurrent-user capacity in any load discussion (Section 34).

### Coverage mechanics

`project_public_access_state()` (`app.py:2099-2132`) implements exactly the locked rule:

```
is_live = project.is_active AND (owner has_active_subscription() OR an ACTIVE coverage row applies)
```

with `_project_specific_coverage_candidates` requiring `status == "ACTIVE" AND coverage_start <= now
AND (coverage_end IS NULL OR coverage_end > now)` (`app.py:1929-1937`). Five coverage sources are
modelled: `OWNER_SUBSCRIPTION`, `STANDALONE_PROJECT_RENEWAL`, `TRANSFER_CARRY_OVER`, `ADMIN_GRANT`,
`LEGACY_COMPATIBILITY` (`models.py:849-855`). Enforced at 13 public surfaces (`app.py:9445`, `9461`,
`9476`, `9503`, `9653`, `9777`, `10500`, `10611`, `10690`, `13316`, `13339`, `13362`).

**Admin-owned projects are not modelled at all.** The owner resolution at `app.py:2110` reads only
the two user-ownership columns, so an admin-owned project can never establish `OWNER_SUBSCRIPTION`
coverage and has no other source by default — see **P0-9**. This is the one place where the
otherwise-correct coverage design has a hole, and it is caught by a currently-failing test.

**Expiry deletes nothing** — verified. It is purely a read-time comparison; there is no sweeper and
no cascade. This satisfies the locked rule.

One defect: `ProjectServiceCoverage.EXPIRED` is in the status vocabulary (`models.py:856`) and is
**written by no code path**. Rows remain `ACTIVE` with a past `coverage_end` forever. Correctness is
preserved because every read compares dates, but any query or Admin view that filters on
`status == 'ACTIVE'` alone will overcount (P1-28).

### Verdict on "Project public availability = Project active AND valid coverage"

`PASS`. This is implemented correctly and consistently, and it is the single most important
commercial invariant in the product. It should not be touched by the entitlement work.

---

## 19. Payments / Add-ons Audit

### Razorpay

| Area | Verdict | Evidence |
| --- | --- | --- |
| Server-side pricing | `PASS` | `amount_paise = int(plan.effective_price * 100)` from the DB (`app.py:8705`); client sends only `plan_id` (`:8674`). Currency from `plan.currency` (`:8716`) |
| Order persistence | `PASS` | `amount`, `total_amount`, `purchased_project_limit`, `purchased_scan_limit` snapshotted (`app.py:8746-8752`) |
| Browser signature verification | `PASS` | SDK `verify_payment_signature` (`app.py:8816-8823`), then ownership-bound (`payment_order.user_id != user.id` → reject, `:8826-8828`), then client-supplied `plan_id`/`amount` cross-checked against the stored row (`:8833-8841`) |
| Webhook signature | `PASS` | HMAC verified over **raw bytes before any JSON parse** (`app.py:9079` vs parse at `9098`); dedicated `RAZORPAY_WEBHOOK_SECRET` with no fallback to the API secret; **fails closed** when unset (`:9082-9086`) |
| Replay / idempotency | `PASS` | Deterministic idempotency key (`app.py:9130-9140`); insert-first gate with `IntegrityError` → `attempt_count += 1` → 200 `{"replay": true}` (`:8910-8941`, `9142-9149`); unique index in `ebeab1cf4ec9:56-59`; `razorpay_order_id` / `razorpay_payment_id` unique in `bc5642a86981:70-78` |
| Duplicate events | `PASS` | same mechanism; unsupported event types acknowledged with zero mutation |
| Concurrent verification | `PASS` | Single conditional UPDATE `WHERE status='pending'` (`app.py:8611-8622`); the loser rolls back, re-reads, and returns an idempotent `replay: true` (`:8624-8630`). Browser and webhook both route through one `activate_payment()` — activation is never reimplemented |
| Webhook hardening | `PASS` | amount check (`9190-9201`), currency check (`9203-9206`), payment-id conflict detection (`9208-9222`), non-`captured` ignored (`9168-9171`), unknown order never auto-creates entitlement (`9177-9180`) |
| Failure behaviour | `PARTIAL` | fails closed on missing config; but four routes leak `str(e)` (P1-02) |
| Subscription activation | `FAIL` | P0-1 |
| Order-create idempotency toward Razorpay | `MISSING` | no `Idempotency-Key` header (P2-14) |
| Sub-₹1 plans | `MISCONFIGURED` | paise floor `max(100, …)` vs webhook amount reconciliation (P2-13) |

### Add-ons

`ADDON_TYPES` = `EXTRA_SCANS`, `VALIDITY_EXTENSION`, `PROJECT_CAPACITY`, `PROJECT_SERVICE_COVERAGE`
(`models.py:629`). `AddonCatalog` carries `scan_delta`, `validity_days_delta`, `project_delta`
(`:644-646`) — **no storage delta**. `PROJECT_SERVICE_COVERAGE` reuses `validity_days_delta` as its
duration (`app.py:7767`).

Fulfilment (`_apply_entitlement_transaction`, `app.py:7784-7837`) is idempotent by DB constraint,
with a pre-check plus `IntegrityError` recovery (`fulfill_addon_purchase`, `:7840-7905`), reachable
from both the browser verify route and the webhook. **Materialisation is a read-modify-write on the
User row** (`:7812`, `:7823`) rather than a conditional UPDATE, so two concurrent fulfilments of
*different* purchases can lost-update the counter even though the ledger rows are both correct.

Two blockers, already stated: the CHECK constraint (P0-2) and the missing catalogue seed/CRUD
(P0-3).

### Fit of a reusable storage add-on

Assessed against the existing architecture: **it fits, with two required changes.**

* `ADDON_TYPES` / `ENTITLEMENT_TYPES` gain a storage type, and `ck_addon_catalog_type` must be
  amended (which is required anyway for P0-2).
* `AddonCatalog` gains a `storage_bytes_delta` **`BigInteger`** column, and
  `EntitlementTransaction.delta_value` must become `BigInteger` — the current `Integer` caps at
  ~2.1 GB and would silently overflow a realistic storage grant. This is the single most important
  schema detail in the storage work.
* The ledger's `expires_at` (already present, never written) gives "purchased storage survives
  upgrade/downgrade unless the entitlement explicitly expires" for free.
* The locked add-on policy is respected by construction: the approved concepts (extra project
  capacity, extra scans, project service coverage, reusable storage) map onto entitlement types;
  the prohibited ones (larger per-file size, longer duration, unlocking a playback mode, more pairs
  per project) map onto **plan columns** and must never be given an `AddonCatalog` row.

---

## 20. Refund Audit

| Requirement | Verdict | Evidence |
| --- | --- | --- |
| Admin-only | `PASS` | all five routes permission-gated; mutating ones require `admin.payments.refund`, held **only** by superadmin (`app.py:12066`, `12089`, `1489-1507`) |
| Full refund only | `PASS` | `_call_razorpay_full_refund` always sends `_refund_amount_paise(refund.amount)` (`app.py:8104-8115`, `7956-7957`); no partial-amount parameter is accepted anywhere |
| No user self-service refund | `PASS` | no user-facing refund route exists |
| States `REFUND_REQUESTED` / `PROCESSING` / `REFUNDED` / `FAILED` | `PASS` | `models.py:552`, validated at `:613-615`, DB CHECK in `b2c4d6e8f0a1:55-60` |
| Reconciliation `PENDING` / `APPLIED` / `MANUAL_REVIEW_REQUIRED` / `FAILED` | `PASS` | `models.py:553-558` |
| Provider confirmation | `PASS` | `_provider_refund_status_to_local` (`app.py:8118-8124`); webhook fallback lookup by `provider_payment_id` + `REFUND_PROCESSING` (`:9009`) |
| Idempotency | `PASS` | four layers: derived `idempotency_key` + pre-check (`app.py:8231-8234`), eligibility gate (`8236-8242`), `IntegrityError` recovery (`8246-8253`), and reconciliation short-circuit on `{APPLIED, MANUAL_REVIEW_REQUIRED}` (`8137-8138`) |
| Double refund impossible | `PASS` | `uq_payment_refunds_payment_order_id` and `uq_payment_refunds_addon_purchase_id` make one refund per source structurally impossible; a CHECK enforces exactly one source (`models.py:564-574`) |
| Webhook replay | `PASS` | shares the `RazorpayWebhookEvent` idempotency gate |
| Capacity reversal | `PASS` for `PROJECT_CAPACITY` | reversal ledger row + `subscribed_project_limit -= delta` (`app.py:8186-8187`) |
| Scan entitlement reversal | `PASS` | reversal ledger row + `subscribed_scan_limit -= delta` (`app.py:8167-8185`) |
| Project coverage reversal | `PASS` | coverage → `REVOKED` with `revoked_at` and `revoked_by_refund_id`; `FAILED` if it was not `ACTIVE` (`app.py:8188-8197`) |
| Subscription reversal | `FAIL` | order marked `refunded`; **subscription dates and limits untouched**; reconciliation set to `MANUAL_REVIEW_REQUIRED` (`app.py:8139-8145`). Money back, service retained, until a human intervenes — and nothing surfaces the queue (P1-29) |
| `VALIDITY_EXTENSION` reversal | `PARTIAL` | also `MANUAL_REVIEW_REQUIRED`; expiry untouched (`app.py:8155-8159`) |
| Launch-capacity reversal | `MISSING` | no refund path touches `CapacityConfig` / `PaymentReservation` (P1-30) |
| Audit history | `PASS` | every transition audit-logged (`app.py:8256`, `8264`, `8272`, `8289-8298`) |
| Failure safety | `PARTIAL` | money moves before the DB records it (P1-31); `except Exception` around reconciliation returns HTTP 200 with `reconciliation_status="FAILED"` (`app.py:8281-8287`, `12078`) |
| No unrelated project/media deletion | `PASS` | verified by enumerating all four callers of `_delete_project_files_and_rows`; no refund path reaches it |

Eligibility codes are thorough: `NOT_FOUND`, `REFUND_ALREADY_PROCESSING`, `REFUND_PREVIOUSLY_FAILED`,
`ALREADY_REFUNDED`, `PAYMENT_NOT_SUCCESSFUL`, `PROVIDER_PAYMENT_MISSING` (`app.py:7976-7989`), plus
add-on-specific `COVERAGE_NOT_ACTIVE`, `SUPERSEDED_BY_LATER_RENEWAL`, `INELIGIBLE_CONSUMED_SERVICE`
(`:8043-8060`). Reason is mandatory (`:8226`).

---

## 21. Reporting / Moderation Audit

| Requirement | Verdict | Evidence |
| --- | --- | --- |
| Public report action | `PASS` | `POST /api/projects/<id>/report` (`app.py:8479-8527`) |
| Anonymous reporting | `PASS` | no `@login_required`; `current_user()` is `None`-tolerant (`:8509-8513`) |
| Reason codes | `PASS` | eight validated reasons (`models.py:1336-1345`), rejected if not in the set (`app.py:8501-8503`) |
| Optional details | `PASS` | length-capped by `CONTENT_REPORT_DETAILS_MAX` (`:8505-8506`) |
| Report storage | `PASS` | `ContentReport` (`models.py:1356-1377`) with `(project_id, status)` and `created_at` indexes |
| Reporter hashing | `PASS` | `_privacy_hash` = `sha256(secret + ":" + value)` (`app.py:8471-8476`); applied to session and IP (`:8487-8488`) |
| Raw IP leakage | `PASS` | raw IP is never persisted. `reporter_email` is stored in the clear when supplied — acceptable, since it is voluntarily provided contact detail |
| Duplicate / spam handling | `PARTIAL` | rate-limited but not collapsed; no per-project duplicate detection |
| Rate limiting | `PARTIAL` | 5/3600 s per IP+project+session (`app.py:280`, `8490-8498`) — **process-local**, so the real budget is `5 × workers` and resets on deploy |
| States `OPEN` / `UNDER_REVIEW` / `ACTION_TAKEN` / `DISMISSED` | `PASS` | `models.py:1346`; `OPEN` explicitly rejected as a review target so reports cannot be reopened (`app.py:12499`) |
| Admin queue | `PASS` | `/admin/moderation` shell (`app.py:12450`) + list (`12467`), detail (`12485`), review (`12492`) |
| Permissions | `PASS` | `admin.reports.view` to read, `admin.reports.manage` to act; UI gated by `CAN_MANAGE` with a read-only banner (`moderation.html:31,133`) |
| Moderation action | `PASS` | five validated actions; `ACTION_TAKEN` requires a valid action (`app.py:12502`) |
| Project suspension | `PASS` | `project.is_active = False` only (`app.py:12515-12519`), which short-circuits `project_public_access_state` at `:2103-2104` |
| Non-destructive | `PASS` | no media deletion, no creator ban — documented at `app.py:12420-12429` and verified against the delete call sites |
| Audit history | `PASS` | `reviewed_by_admin_id` / `reviewed_at` on the row plus `AdminActivity` (`app.py:12522-12530`) |
| Reporter privacy in the admin payload | `PASS` | `_content_report_payload` (`app.py:12430-12447`) deliberately omits `reporter_email`, `reporter_ip_hash`, `reporter_session_hash`, exposing only `has_reporter_contact` |

Three defects: unordered status transitions allow repeated re-suspension (P1-32); the hash salt is
`app.secret_key`, so rotation orphans every hash (P1-33); and `content_reports` cascades from
`Project`, so deleting a project destroys its moderation history (P1-34). Also note that public
media carries `Cache-Control: public, max-age=3600`, so a suspended project's media may remain in a
browser cache for up to an hour — already documented at `docs/production/README.md:133`.

---

## 22. PostgreSQL / Schema / Index / Transaction Audit

### Structure

~40 models. Constraints and indexes are used deliberately, not decoratively: `UniqueConstraint` on
`(project_id, pair_index)`, `(user_id, scan_session_id)`, `(source_type, source_id,
entitlement_type)`, `(workspace_id, idempotency_key)`, `(project_id, idempotency_key)`,
`(user_id, consent_type, policy_version, source_context)`; four unique constraints plus an
exactly-one-source CHECK on `payment_refunds`; `CheckConstraint`s on `upload_sessions` offsets,
experience type and playback mode. Composite indexes exist where the queries need them
(`ix_project_service_coverages_project_status_end`, `ix_content_reports_project_status`,
`ix_processing_jobs_*`, `ix_project_ownership_transfers_*`).

The `use_alter=True` on `Project.fallback_pair_id` (`models.py:900-904`) correctly breaks a cyclic
FK for `create_all()` ordering, and the reasoning is documented in the model.

### Concerns

| ID | Concern | Evidence |
| --- | --- | --- |
| S-01 | **No `ondelete` behaviour is declared on any foreign key** in the entire model file. Every FK is PostgreSQL default `NO ACTION`, so ORM-level cascades and DB-level integrity disagree wherever the ORM is bypassed | `models.py` throughout |
| S-02 | `UploadSession.project_id` / `pair_id` have no cascade and are never cleared → **P0-5** | `models.py:1982-1983` |
| S-03 | `User.payment_orders` is `cascade="all, delete-orphan"` while `PaymentRefund.payment_order_id` is not → deleting a user destroys financial records and violates the refund FK. Latent only because no user-deletion route exists | `models.py:197`, `577` |
| S-04 | `Admin.projects` is `cascade="all, delete-orphan"` → deleting an admin deletes their projects' **rows**, with no call to `_delete_project_files_and_rows`, orphaning 100% of their media | `models.py:800` |
| S-05 | `SubscriptionPlan.users` has no `ondelete`; plan deletion is guarded in the route (`app.py:11776`) but not at the DB level | `models.py:67` |
| S-06 | `ProjectPair.image_size` / `video_size` and `Asset.size_bytes` are `Integer` (~2.1 GB cap); `EntitlementTransaction.delta_value` is `Integer` and would overflow for storage byte deltas | `models.py:1048-1049`, `699`, `1727` |
| S-07 | `EntitlementTransaction.expires_at` exists and is written by no code path — the field a governed finite Admin grant needs | `models.py:705` |
| S-08 | `Admin.permissions_json` exists and is read by no code path (P1-13) | `models.py:783-786` |
| S-09 | `Project.beneficiary_user_id` exists and is read by no code path (P2-03) | `models.py:871` |
| S-10 | `ProjectPair.image_hash` is present as a commented-out column, suggesting an abandoned dedupe design | `models.py:1045` |

### Transactions and concurrency

| Pattern | Verdict |
| --- | --- |
| Project / scan quota, launch capacity, payment activation, reservation flips, `ScanLog.counted` claim, upload finalize gate | `PASS` — all are single conditional UPDATEs whose `updated == 1` result is the decision. This is the right pattern and it is applied consistently |
| Pair quota | `PASS` on PostgreSQL via `SELECT … FOR UPDATE`; unserialised on SQLite (`TEST-GAP`) |
| Add-on materialisation | `PARTIAL` — ledger insert is DB-idempotent, but the counter update is a read-modify-write (Section 19) |
| Transfer `projects_used` decrement | `PARTIAL` — read-modify-write (`app.py:1859`) |
| `User.increment_scans_used()` | `MISCONFIGURED` — read-modify-write **plus an in-model `db.session.commit()`** (`models.py:285-291`). The same anti-pattern appears in `mark_feature_extraction_complete`, `mark_video_compression_complete`, `mark_as_failed`, `increment_match_count` (`models.py:1154-1194`): models commit the session, which breaks caller transaction boundaries. The live scan path correctly uses `_consume_scan_quota_atomic` instead, so this is a latent trap rather than a live bug |

### `Query.get()` deprecation

`SubscriptionPlan.query.get()`, `User.query.get()`, `Project.query.get()` and similar legacy
`Query.get()` calls are used throughout `app.py`. These produce the SQLAlchemy 2.0 `LegacyAPIWarning`
noted in the audit brief's baseline. Cosmetic today; a real migration blocker when SQLAlchemy 2.0 is
adopted. `P2-POST-GO-LIVE`.

---

## 23. Alembic / Migration Audit

**Current head: `b2c4d6e8f0a1`. Single head. 15 revisions, strictly linear**, verified structurally:
every `down_revision` value appears exactly once, exactly one revision has `down_revision = None`
(`3914ece79b88`), exactly one revision is never referenced as anyone's parent, and all 15 declare
`branch_labels = None`.

```
3914ece79b88 baseline current schema
 └ bc5642a86981 razorpay id unique constraints
  └ 54a108a17fa7 capacity config + payment reservations
   └ ebeab1cf4ec9 razorpay webhook events
    └ a73f2c19d8e2 processing job rq foundation
     └ 44340c16353c resumable upload sessions
      └ 0b8fffb4c614 fallback video data model + scan events
       └ d6b9c1f4a2e8 user consent evidence
        └ f4a8c2b91d70 addon entitlement foundation
         └ b7c9d2e4f6a1 project experience type
          └ c8d1e2f3a4b5 project playback mode
           └ d2a4b6c8e0f1 domain ownership + service coverage foundation
            └ e5f6a7b8c9d0 upload session experience contract
             └ a1c3e5b7d9f2 project targeted entitlements + content reports
              └ b2c4d6e8f0a1 admin payment refunds  (HEAD)
```

`scripts/production/verify_alembic_state.ps1:37-53` already enforces single-head and
DB-revision-equals-app-head at ops time — good practice worth keeping.

**Downgrades: all 15 are real implementations**, none is a `pass` stub. All column and constraint
alterations correctly use `op.batch_alter_table` for SQLite compatibility.

**`migrations/env.py`** takes the URL from the live Flask-SQLAlchemy engine rather than the
environment (`:18-39`), with a `get_engine()` fallback for Flask-SQLAlchemy < 3 — the source of the
deprecation warning in the brief's baseline. Batch mode is decided per connection from the real
dialect name (`:54-67`, `:121`). Two notes: the comments describe production as **MySQL** (P2-05),
and `render_as_string(hide_password=False)` (`:29-31`) places the DB password into Alembic's config
— not logged by default, but one verbose logger away from exposure.

### Risks

| ID | Migration | Risk |
| --- | --- | --- |
| M-01 | `f4a8c2b91d70:34` | `ck_addon_catalog_type` omits `PROJECT_SERVICE_COVERAGE` and is never amended — **P0-2** |
| M-02 | `bc5642a86981:57-78` | Converts two indexes to `UNIQUE` with a preflight that **raises** on existing duplicates and offers no cleanup path — the most likely mid-upgrade abort on a populated DB (P1-38) |
| M-03 | `b7c9d2e4f6a1` downgrade | `UPDATE project_pairs SET image_filename = '' WHERE image_filename IS NULL` — irreversibly destroys Direct-QR NULL semantics. The only data-mutating migration in the chain |
| M-04 | `ebeab1cf4ec9` vs `f4a8c2b91d70:99-101`, `b2c4d6e8f0a1:77` | `razorpay_webhook_events` is created without `addon_purchase_id` / `payment_refund_id`, added by later revisions. Correct as a chain, but it means the table shape depends on how far the chain has been applied — relevant to any partial-upgrade rollback |
| M-05 | model vs migration drift | The `ck_addon_catalog_type` case proves the two can diverge undetected because tests use `create_all()`. **A migrated-schema test lane is the systemic fix**, not a one-off constraint patch |

### Seeding and bootstrap

* Startup bootstrap (`app.py:973-1039`) is correctly gated: `SCANSTORY_SKIP_STARTUP_BOOTSTRAP`
  defaults to `True` outside testing, and `db.create_all()` outside tests raises a `RuntimeError`
  instructing the operator to run migrations (`:975-979`).
* Seeded plans (Free Trial 1 project / 50 scans; Basic 5 / 500; Pro 20 / 2000) **never set
  `max_pairs_per_project`**, so they silently inherit the model default of 10 — while
  `/admin/plans/add` **requires** the field (`app.py:11576-11588`). Inconsistent.
* A **second, duplicated** seed of the same three plans exists in `bootstrap_database()`
  (`app.py:3647-3700`) and is *not* gated by `SCANSTORY_SKIP_STARTUP_BOOTSTRAP` (P2-07).
* **`AddonCatalog` is never seeded anywhere** — P0-3.
* Bootstrap admin (`app.py:1043`, `907-938`) is env-gated with no default credential, and the
  historical committed backdoor has been removed. `PASS`.

---

## 24. Redis / RQ Audit

| Area | Verdict | Evidence |
| --- | --- | --- |
| Redis URL config | `PASS` | `REDIS_URL` with **no default fallback** — absent means "not configured", never `localhost` (`processing_queue.py:42` et al.) |
| Queue name / timeout | `PASS` | `RQ_QUEUE_NAME` default `scanstory-processing`; `RQ_DEFAULT_TIMEOUT` default 600 s, non-int raises `QueueUnavailable` (`:54-62`) |
| Queue modes | `MISCONFIGURED` | `{fake, inline, rq}`; resolution falls through to **`fake`** when no production signal and no `REDIS_URL` — **P0-6** |
| Production enforcement | `PASS` (conditional) | `app.py:133-140` requires `queue_mode()=="rq"` and `REDIS_URL`; `rq_worker.py:14-17` enforces the same. Depends entirely on a production env flag being set |
| Enqueue | `PASS` | `queue.enqueue("processing_operations.run_processing_job", job.id, job_timeout=…, retry=…)` after a real `conn.ping()` (`processing_queue.py:154-164`) |
| Duplicate jobs | `PASS` | `ProcessingJob.idempotency_key` with unique constraints on `(workspace_id, key)` and `(project_id, key)` |
| Failure handling | `PASS` | enqueue failure sets `status="failed"`, `safe_error_code="QUEUE_UNAVAILABLE"` and re-raises (`:170-206`) |
| Enqueue failure at the app layer | `PARTIAL` | `_schedule_project_pair_processing` catches `QueueUnavailable`, logs, and flashes a user error — **the request still succeeds** (`app.py:2297-2325`), leaving an unprocessed project |
| Retry behaviour | `PARTIAL` | `Retry(max=attempts-1, interval=[30,120,300,900])` (`:157-158`) but `rq_worker.py:29` runs `work(with_scheduler=False)` — delayed retries need RQ's scheduler (P1-24) |
| Failed / stale jobs | `PARTIAL` | `recover-processing-jobs` CLI exists (`app.py:3350-3399`, dry-run by default) and is never scheduled; `retry_failed_job` exists and is never called (P1-23) |
| Worker assumptions | `PASS` | `rq_worker.py` is Linux-appropriate; the Windows `SimpleWorker`/`TimerDeathPenalty` helper documented as "must never ship" is **confirmed absent** from this worktree |
| Windows-only code | `PASS` (scoped) | `processing_worker.py:11-14` reconfigures stdout encoding and `:41-43` refuses any non-SQLite URL — a local Gate E tool, not the production path |
| Readiness behaviour | `PARTIAL` | `redis_ready_check()` does a real `ping()` in `rq` mode but returns `True` unconditionally for `fake`/`inline` (`processing_queue.py:300-316`) — P0-6 |
| Admin visibility | `PARTIAL` | `_rq_diagnostics_payload` (`app.py:12858-12891`) reports availability, mode, queue name, timeout, pending/running/failed. **Missing: worker count and oldest waiting job age** (Section 12.2) |

---

## 25. SMTP Audit

| Area | Verdict | Evidence |
| --- | --- | --- |
| Host / port / security / timeout | `PASS` (validation) | `SMTP_HOST` (`app.py:2363`); `SMTP_PORT` validated 1-65535 with **no default** (`core/config.py:36-46`); `SMTP_SECURITY` default `starttls` with alias normalisation (`:49-62`); `SMTP_TIMEOUT_SECONDS` default 10 s, must be finite and positive (`:23-33`). Validated at every startup |
| TLS enforcement | `MISCONFIGURED` | `SMTP_SECURITY=none` is permitted with **no production guard**; production validation checks only that the five SMTP vars are present (`app.py:130-132`) — P1-26 |
| Certificate verification | `PASS` | `ssl.create_default_context()` (`app.py:2382`) |
| Sender | `PASS` | `MAIL_FROM` falls back to `SMTP_USER` (`:2367`) |
| Credentials | `PASS` | env-only, never logged, never surfaced in diagnostics |
| Emails sent | `PASS` (inventory) | verification OTP (`:2390`), reset OTP (`:2394`), payment success (`:2406`), admin reset (`:2417`), contact form (`:5390`) |
| Synchronous in request path | `MISCONFIGURED` | no queue, no thread — a request can block for the full timeout per send (P1-25) |
| Retries | `MISSING` | none anywhere |
| Failure handling | `PARTIAL` | three inconsistent styles. Registration is best-behaved (invalidates the OTP row and flashes an honest error, `:5779-5789`); forgot-password deliberately flashes success for enumeration resistance but logs via `print()` (`:6047-6053`); payment-success loss is silent (`:8859-8863`). Only the webhook path uses the logger properly (`:9244`) |
| Header injection | `PARTIAL` | contact-form `Subject` interpolates raw `name` and `enquiry_label` with no newline stripping (`app.py:5361` → `2378`). `to_email` is hard-coded, limiting blast radius, but explicit `\r\n` rejection is warranted — P1-27 |
| Template safety | `PARTIAL` | four of five emails use autoescaped `render_template`; the contact-form body is a raw f-string interpolating user input into HTML including `href="tel:…"` and `href="mailto:…"` attributes (`:5362-5386`) — P1-27 |
| Tracked send state | `MISSING` | `_smtp_diagnostics_payload` (`app.py:12832-12856`) reports configuration booleans only. No last-success, no last-failure, no counter — an operator cannot tell whether mail has ever been delivered |

---

## 26. Health / Readiness / Observability Audit

**`/healthz`** (`app.py:577-581`) returns a static `{"status":"ok"}` with `Cache-Control: no-store`.
It checks **nothing** — correct as a liveness probe and consistent with
`docs/production/monitoring-alerting.md:5-9`.

**`/ready`** (`app.py:584-616`) executes `SELECT 1` and, **only when `queue_mode() == "rq"`**, a real
Redis `ping()`. Returns 503 with `{"status":"not_ready"}` on failure, rolls back the session, logs
`readiness_check_failed` server-side, and never leaks exception text.

**Not verified by readiness:** RQ **worker liveness** (Redis up ≠ a worker consuming), media
directory writability (zero `os.access`/`W_OK` hits in application code — `app.py:678` only calls
`os.makedirs(exist_ok=True)` at import), migration revision vs Alembic head, and disk space.

**Three ways health can be green while the system is broken:**

1. `SCANSTORY_QUEUE_MODE=fake` or `inline` skips the queue branch entirely → 200 ready with zero job
   processing (**P0-6**).
2. RQ worker process down while Redis is up → 200 ready, queue depth grows unbounded.
3. Media directory read-only or disk full → 200 ready, uploads fail at request time.

**Logging** is two lines: `logging.basicConfig(level=logging.DEBUG)` and
`app.logger.setLevel(logging.DEBUG)` (`app.py:94-95`), unconditional in production. No file handler,
no rotation, stderr only. `LOG_LEVEL` and `STRUCTURED_LOGGING_ENABLED` are documented env vars that
**no code reads**. The three structured-telemetry helpers pass allowlisted dicts via `extra=` with
no JSON formatter installed, so **the payloads are silently discarded** and only the bare event name
reaches stderr (P1-04) — careful instrumentation producing no output. There are no request or
correlation IDs, and 156 `print()` calls act as a parallel logging channel.

**Centralized error monitoring: `MISSING`.** No Sentry, OpenTelemetry, Datadog, or New Relic in
`requirements.txt` or anywhere in the repo. Error aggregation is `app.logger.exception()` to stderr
(`app.py:13449`) — nothing leaves the host. For a payment-handling production service this is the
single largest observability gap.

**Error-handling hygiene is otherwise good:** the global handler never returns `str(error)` or a
traceback, the CSRF handler logs the real reason and returns a generic message, and error sanitisers
(`safe_error_summary`, `sanitize_error`) strip paths and redact `secret|token|password|signature=`
patterns with test coverage. The five leaking route-local handlers (P1-01, P1-02) are the exception,
not the rule.

---

## 27. Privacy / Consent / Retention Audit

| Item | Verdict | Evidence |
| --- | --- | --- |
| Terms + Privacy acceptance captured | `PASS` | `UserConsentEvidence` (`models.py:308-341`) with `consent_type`, `policy_version`, `accepted_at`, `source_context`, `evidence_metadata` and a unique constraint preventing duplicates |
| Policy version | `PASS` | `SCANSTORY_TERMS_POLICY_VERSION` / `SCANSTORY_PRIVACY_POLICY_VERSION` (`app.py:4776-4787`), default `v1` |
| Timestamp | `PASS` | `accepted_at`, indexed |
| Camera explanation | `PASS` | the scanner requests camera only on an explicit Start Camera press, with a pre-camera intro/target guide (`templates/user/scanner.html:570,879-894`); `Permissions-Policy: camera=(self)` is set |
| Report privacy | `PASS` | raw IP never persisted; hashed identifiers; admin payload omits reporter contact fields |
| Hashed identifiers | `PARTIAL` | salted with `app.secret_key`, so secret rotation orphans every hash (P1-33) |
| User self-service deletion / deactivation | `MISSING` | no route exists. The only user-deletion code is the CLI dev-test cleanup (`app.py:3499-3528`), gated to development. Any future implementation must contend with S-03 (cascade to `payment_orders`) and the absence of any media-ledger manifest |
| Project / media deletion | `PARTIAL` | user and admin delete routes exist; the admin-owned path deletes no files (P0-4) and there is no orphan reconciliation |
| Payment / refund record retention | `UNTESTED` (policy) | records persist indefinitely; no retention policy is expressed in code or docs. **Requires business/legal signoff** |
| Audit / moderation retention | `PARTIAL` | `AdminActivity` grows without bound and has no retention policy; `content_reports` is destroyed by project deletion (P1-34) |
| Log PII | `MISCONFIGURED` | `_otp_log` writes user emails at INFO under an unconditional DEBUG root level with no rotation (P1-05); `docs/production/monitoring-alerting.md:83` forbids customer email in payment-order logs, and this is a different but adjacent path |
| `otp_codes`, `user_login_activities`, `admin_activities` retention | `MISSING` | `scripts/migration/sqlite_to_postgresql_rehearsal.py:29-33` explicitly flags exactly these three tables for policy review — the repo has already identified the question and not answered it |

**Requires legal / business signoff (not an engineering decision, and this audit makes no
compliance claim):** backup retention duration; payment and refund record retention; audit-log
retention; moderation-record retention; OTP and login-activity retention; whether user-initiated
account deletion must be offered and what it must erase versus retain for financial-record
obligations; and the lawful basis for storing hashed reporter IPs.

---

## 28. Backup / Restore / Rollback Audit

**Local/code scope only. No production backup claim is made or possible from here.**

| Item | Verdict | Evidence |
| --- | --- | --- |
| Backup scripts | `MISSING` | **no executable backup tooling exists in the repository** — no `pg_dump`, no media sync, no restore script, no filename convention |
| Backup runbook | `PASS` (documentation) | `docs/production/backup-restore-runbook.md` scopes DB + marker images + videos + feature artifacts + QR assets + secrets + Alembic version (`:6-13`), frequency "at least daily plus pre-deployment" (`:17-18`), encrypted off-host copies (`:19-21`), consistency point (`:23-27`), integrity verification including a sample restore and a representative media decode (`:29-36`), and a restore rehearsal (`:38-48`) |
| Retention | `SERVER-TEAM-VERIFY` | explicitly deferred to business policy; the repo declines to invent a number, which is correct |
| DB dump assumptions | `SERVER-TEAM-VERIFY` | no tooling, no documented dump format |
| Media backup assumptions | `SERVER-TEAM-VERIFY` | filesystem paths are known (`SCANSTORY_DATA_DIR`, `SCANSTORY_ADMIN_DATA_DIR`), but nothing in the repo copies them |
| Restore validation | `MISSING` (structural) | with no media ledger there is **no manifest** against which to verify that every DB-referenced file returned. This is a direct consequence of Section 9 |
| Migration rollback | `PASS` (mechanism) | all 15 downgrades are real; `docs/production/rollback-runbook.md:44` and `database-migration-runbook.md:103` both forbid `flask db downgrade base` in production. Caveat M-03: `b7c9d2e4f6a1`'s downgrade destroys Direct-QR NULL semantics |
| Rollback package expectations | `PASS` (documentation) | app rollback, migration/data rollback, media rollback, credential rotation, and a named rollback authority (`rollback-runbook.md:21-61`; `README.md:116`) |
| Media persistence assumptions | `SERVER-TEAM-VERIFY` | the app assumes a durable local filesystem at `SCANSTORY_DATA_DIR`; whether that is a persistent volume, its size, and its backup coverage cannot be determined locally |
| Deployment runbook | `PASS` | 30 ordered steps with `/healthz` and `/ready` baselines and seven stop conditions (`deployment-runbook.md:8-54`) |
| Read-only ops scripts | `PASS` | `scripts/production/` provides five verification scripts that print no secret values, do not deploy, do not restart, and do not run migrations |

**Documentation drift to correct alongside V1.1:** `docs/production/README.md:124,131-132` states
"queue monitoring is future until Redis/RQ exists" and "there is no automatic refund flow" — both
shipped (P1-37).

---

## 29. Maintainability / Code-Structure Findings

**This is not a rewrite recommendation.** The architecture is a Flask monolith and V1.1 keeps
Flask. The findings below are about seams, not frameworks.

1. **`app.py` is 13,522 lines** containing routing, domain logic, entitlements, payments, refunds,
   ownership, moderation, upload orchestration, scanner endpoints, admin surface, CLI commands, and
   config. Two agents working in parallel on this branch is already evidence of the merge-contention
   cost. The natural extraction seams, in the order they pay for themselves:
   * the entitlement/capacity/coverage helpers (`app.py:2549-3162`) — self-contained, heavily
     tested, and about to grow substantially in V1.1;
   * the refund and add-on fulfilment block (`app.py:7732-8300`);
   * the ~15 `@app.cli.command` definitions, which have no reason to sit in the request module.
   Extraction should be mechanical and behaviour-preserving, done **after** the P0 fixes, not
   interleaved with them.
2. **Two sources of truth for media limits.** `upload_validation.py` (live) and
   `media_processing.py` (dormant Experience Creator, different numbers). The split is documented,
   but it is a trap during the V1.1 media-policy work.
3. **A dormant parallel data model.** Nine tables for Organization/Workspace/Experience/Trigger/
   Asset behind eight always-false flags. Keep or delete deliberately — but if the storage work
   proceeds as recommended in Section 9, do not entangle it with them.
4. **Dead code and dead fields**: `compress_video` (zero callers), `beneficiary_user_id`,
   `Admin.permissions_json`, `super_admin_required`, `EntitlementTransaction.expires_at` (never
   written), `ProjectServiceCoverage.EXPIRED` (never written), commented-out
   `ProjectPair.image_hash`. Several are actively dangerous (P1-13) rather than merely untidy.
5. **Root-level one-off scripts** (`fix_limits.py`, `migration_script.py`, `migration_gate_c.py`,
   `gate_c_migration.py`, `gate_d_*.py`, `gate_e_inputs.py`, `add_simple_admin.py`) sit beside the
   application. `fix_limits.py` in particular encodes a historical `999999` sentinel that still
   contradicts the current `None`/`0` convention (Section 11).
6. **Models commit the session.** `models.py:285-291` and `1154-1194` call `db.session.commit()`
   inside model methods, breaking caller transaction boundaries. The live paths avoid them, but
   they are landmines for new code.
7. **Duplicated bootstrap seed** (`app.py:986-1039` and `3647-3700`) with divergent gating.
8. **`print()` as a logging channel** — 156 occurrences (P1-05).
9. **Positive note worth preserving:** the codebase is unusually well commented *where it matters*.
   The docstrings on `CapacityConfig`, `PaymentReservation`, `RazorpayWebhookEvent`, `ScanEvent`,
   `UploadSession`, and the `use_alter` FK explain **why**, not what, and several record the exact
   trade-off a future maintainer would otherwise re-litigate. That discipline should be extended to
   the entitlement work rather than abandoned under schedule pressure.

---

## 30. Full Anomaly Register

| ID | Severity | Area | Evidence | Impact | Recommended fix |
| --- | --- | --- | --- | --- | --- |
| ANM-01 | BLOCKER | Commercial | `app.py:8638` overwrites `subscribed_scan_limit` with the raw plan value | Purchased `EXTRA_SCANS` destroyed on every activation | Add `reconciled_scan_limit()` mirroring `reconciled_project_limit()` |
| ANM-02 | BLOCKER | Commercial | `app.py:8639-8640` resets `projects_used`/`scans_used` to 0 | Capacity gate bypassed; user gets a fresh allowance on top of existing projects | Stop resetting; preserve usage across plan change |
| ANM-03 | BLOCKER | Migration | `f4a8c2b91d70:34` CHECK omits `PROJECT_SERVICE_COVERAGE` | Coverage/renewal add-on cannot be inserted in production; tests miss it because they use `create_all()` | Amend the constraint; add a migrated-schema test lane |
| ANM-04 | BLOCKER | Commercial | Zero `AddonCatalog(` constructors outside `models.py` | `/api/addons/catalog` returns `[]` in production; add-on system dark | Seed + Admin CRUD |
| ANM-05 | BLOCKER | Storage | `app.py:3188-3190,3201` hard-code user dirs | Admin project delete orphans 100% of its media, silently | Use `processing_operations._dirs_for_project` |
| ANM-06 | BLOCKER | Storage | `app.py:3195-3196,3205-3206` bare `except: pass`, then unconditional commit | DB delete succeeds while unlink fails, invisibly | Log, and gate capacity release on verified deletion |
| ANM-07 | BLOCKER | Schema | `models.py:1982-1983` `UploadSession` FKs never cleared on project delete | PostgreSQL `IntegrityError` on a routine user action; SQLite tests cannot see it | Cascade or explicit detach |
| ANM-08 | BLOCKER | Queue | `processing_queue.py:44` falls through to `fake` | Jobs created and never run while `/ready` returns 200 | Fail closed; make `/ready` mode-aware |
| ANM-09 | BLOCKER | Upload | `app.py:3566-3568` `MAX_CONTENT_LENGTH` unset; no documented `client_max_body_size` | Up to 10 GiB per request spooled to disk before any check | Set a cap and/or evidence the proxy limit |
| ANM-10 | BLOCKER | Abuse | `app.py:10847`, `10895` have no rate limiting | Unthrottled admin password spray and OTP mail-bomb | Add IP limits |
| ANM-10b | BLOCKER | Coverage | `app.py:2110` resolves the owner only via `current_owner_user_id or owner_user_id`; admin-owned projects have both NULL | Every admin-owned project is permanently out of coverage — media, QR and scanner all return unavailable. **Already failing in the full test suite** | Add an `owner_admin_id` branch to `project_public_access_state` |
| ANM-11 | HIGH | Abuse | `rate_limit.py:13-63` process-local; `RATE_LIMIT_REDIS_URL` never read | Every published limit is `×workers` and resets on deploy | Redis-backed limiter |
| ANM-12 | HIGH | Errors | `app.py:5402`, `10472` return `str(e)` from unauthenticated endpoints | SMTP banners and SQLAlchemy schema disclosed | Generic error codes |
| ANM-13 | HIGH | Commercial | UI limits are JS literals (`user_create_project.html:3372,3898`) vs env-driven server limits | Any env override silently desyncs the UI | Inject from server, as `MAX_PAIRS_PER_PROJECT` already is |
| ANM-14 | HIGH | Commercial | `app.py:2986` reads `max_pairs_per_project` live; `8636-8637` snapshots project/scan limits | Two adjacent Admin fields behave in opposite ways with no UI indication; lowering pairs hits live subscribers instantly | Choose one convention; surface it |
| ANM-15 | HIGH | Entitlement | `models.py:252`, `app.py:2638`, `12696` treat `0` as **unlimited** | An admin entering `0` to mean "none" grants unlimited | Use `NULL` for unlimited only; reject or reinterpret `0` |
| ANM-16 | HIGH | Entitlement | Four sentinels for "unlimited": `None`, `999999999`, `999999`, `0` | Guaranteed future logic error | Single convention |
| ANM-17 | HIGH | Entitlement | Admin grants write `subscribed_*` directly (`app.py:11466`, `11907`, `12679`, `12708`) | Effective entitlement cannot be decomposed into base + purchased + grants | Route grants through the ledger, using the existing unused `expires_at` |
| ANM-18 | HIGH | Ownership | Zero HTTP routes for transfer/claim; three helpers have no callers | Locked V1.1 transfer requirements unreachable; `PENDING_CAPACITY` cannot be entered or exited | Build the surface |
| ANM-19 | MEDIUM | Coverage | `app.py:1929-1937` filters coverage by `project_id` only | Coverage bought by a previous owner keeps a project live for a new owner, for all source types | Product decision, then scope to source type |
| ANM-20 | MEDIUM | Coverage | `ProjectServiceCoverage.EXPIRED` never written | Any `status='ACTIVE'` query overcounts | Sweeper or documented read-time-only semantics |
| ANM-21 | MEDIUM | Refund | `app.py:8139-8145` subscription refund revokes nothing | Money returned, service retained, indefinitely | Manual-review queue + defined policy |
| ANM-22 | MEDIUM | Refund | No refund path touches `CapacityConfig`/`PaymentReservation` | Refunded accounts permanently hold launch slots | Decide and implement |
| ANM-23 | MEDIUM | Refund | `app.py:8260-8280` commits `REFUND_PROCESSING`, then calls Razorpay | A crash between leaves an orphan row; no `reconcile-refunds` CLI | Add reconciliation |
| ANM-24 | MEDIUM | Admin | 9 admin JSON endpoints return HTML 302 on auth failure | `response.json()` throws a parse error instead of "session expired" | Content-negotiate in the decorator |
| ANM-25 | MEDIUM | Admin | `admin_delete_own_project` destructive and unaudited (`app.py:13419`) | No forensic record of an admin deleting media | Add `log_admin_activity` |
| ANM-26 | MEDIUM | Admin | Plain admin can set arbitrary scan limits (`app.py:12679`) while the equivalent subscriptions route is superadmin-gated | The superadmin gate provides no real control | Align the gates |
| ANM-27 | MEDIUM | Auth | Lockout keyed on `user_id`, never cleared on success (`app.py:5894-5917`) | Anonymous account-lockout DoS; persistent lockout after a successful login | Re-key and clear |
| ANM-28 | MEDIUM | Auth | `is_blocked` unchecked on four upload routes | A blocked user can finalize and create a Project | Check in `_upload_identity()` |
| ANM-29 | MEDIUM | Auth | reCAPTCHA fails open when unset (`app.py:415-418`) | Silent CAPTCHA bypass on `/register` and `/contact` | Require in production |
| ANM-30 | MEDIUM | Session | No `PERMANENT_SESSION_LIFETIME` anywhere | Sessions never expire | Set a lifetime |
| ANM-31 | MEDIUM | AuthZ | `Admin.permissions_json` defaults to granting everything and is never read | Wiring it later silently promotes every admin | Delete or make authoritative |
| ANM-32 | MEDIUM | Media | `serve_qr` skips the availability gate when the filename does not parse (`app.py:9475-9479`) | Any `QR_DIR` file is publicly readable; suspended QRs may keep serving | Fail closed on unparsed names |
| ANM-33 | MEDIUM | Media | `/admin/image|video|qr` have no auth decorator yet send `Cache-Control: private` | Header implies a restriction that is not enforced | Fix the header; document the accepted risk |
| ANM-34 | MEDIUM | Upload | Client accepts `video/quicktime`; MOV passes `ftyp` and is stored as `.mp4` | Container/extension divergence | Align client and server |
| ANM-35 | MEDIUM | Upload | Multi-pair edit non-atomic (`app.py:5567-5604`) | Files swapped, DB rolled back | Stage all, then commit |
| ANM-36 | MEDIUM | Storage | `image_size`/`video_size` never updated on replacement; multipart uses client `content_length` | Any storage accounting built on them is wrong from day one | Set from `os.path.getsize` on every write |
| ANM-37 | MEDIUM | Schema | `Integer` byte columns cap at ~2.1 GB; `delta_value` likewise | Storage entitlements would overflow | `BigInteger` |
| ANM-38 | MEDIUM | Observability | `extra=` telemetry discarded for want of a formatter (`app.py:342,385,405`) | Instrumentation exists and produces nothing | Install a JSON formatter |
| ANM-39 | MEDIUM | Observability | `logging.basicConfig(DEBUG)` unconditional; no rotation; emails logged | PII-bearing unbounded logs | Env-gate the level; rotate |
| ANM-40 | MEDIUM | Ops | Four correct maintenance CLIs, none scheduled | Orphans, stale reservations, stuck jobs, counter drift accumulate | Schedule them |
| ANM-41 | LOW | Commercial | `subscription_end = now + duration_value * 30` (`app.py:8607`) | "1 Year" grants 360 days | Use real month arithmetic |
| ANM-42 | LOW | Commercial | Upgrade does not chain remaining validity (`app.py:8605-8609`) | Early upgrade forfeits paid days | Chain, as `VALIDITY_EXTENSION` already does |
| ANM-43 | LOW | Concurrency | Add-on materialisation and transfer decrement are read-modify-writes | Lost update under concurrency | Conditional UPDATE |
| ANM-44 | LOW | Concurrency | `models.py:285-291,1154-1194` commit inside model methods | Broken caller transaction boundaries | Remove the commits |
| ANM-45 | LOW | Moderation | Unordered report status transitions | A dismissed report can be re-actioned repeatedly | Define a transition matrix |
| ANM-46 | LOW | Moderation | Reporter hash salted with `app.secret_key` | Secret rotation orphans all hashes | Dedicated salt |
| ANM-47 | LOW | Moderation | `content_reports` cascades from `Project` | Project deletion destroys moderation history | Retain independently |
| ANM-48 | LOW | Schema | `User.payment_orders` cascade vs `PaymentRefund` FK | User deletion would destroy financial records and violate the FK | Resolve before any deletion feature |
| ANM-49 | LOW | Schema | `Admin.projects` cascade deletes rows without deleting files | Full media orphan on admin deletion | Route through the file-aware helper |
| ANM-50 | LOW | Migration | `bc5642a86981` aborts on duplicate Razorpay ids with no cleanup path | Mid-upgrade abort on a populated DB | Add a preflight report and remediation |
| ANM-51 | LOW | Migration | `b7c9d2e4f6a1` downgrade overwrites NULL `image_filename` with `''` | Direct-QR semantics irrecoverable | Document as one-way |
| ANM-52 | LOW | Config | `SCANSTORY_TESTING=1` on a production host permits SQLite and forces `fake` | Silent total degradation | Add to the production prohibition beside `SCANSTORY_DEV_TESTING` |
| ANM-53 | LOW | Config | `DATA_DIR` defaults to the relative `"data"` | CWD-dependent media root | Require an absolute path in production |
| ANM-54 | LOW | SMTP | `SMTP_SECURITY=none` permitted in production | Credentials in plaintext | Guard in production validation |
| ANM-55 | LOW | SMTP | Contact form injects raw input into `Subject` and an unescaped HTML body | Header/HTML injection | Strip CRLF; use a template |
| ANM-56 | LOW | Queue | `with_scheduler=False` vs delayed `Retry` intervals | Delayed retries may never fire | Verify and enable the scheduler |
| ANM-57 | LOW | Queue | `retry_failed_job` imported and never called | No retry path for failed jobs | Wire to an admin action |
| ANM-58 | LOW | Seeding | Seeded plans omit `max_pairs_per_project` while the Admin form requires it | Silent default of 10 | Set explicitly in the seed |
| ANM-59 | LOW | Docs | `docs/production/` states refunds and queue monitoring do not exist | Operators misled during an incident | Update for V1.1 |
| ANM-60 | LOW | Tooling | `run-tests.ps1` hard-codes a repo root and uses bare `python` | Cannot run from a worktree or with the authoritative venv | Parameterise |

---

## 31. Migration & Backfill Plan Required for V1.1 (audit only)

Per-requirement classification. **No migration was written during this audit.**

| Requirement | Change type | Backfill | Notes |
| --- | --- | --- | --- |
| `plan_family` | `NEW COLUMN` on `subscription_plans` | `BACKFILL-REQUIRED` — set all existing plans to `INDIVIDUAL` | Non-null with server default; a CHECK or app-level validation |
| `max_image_bytes` | `NEW COLUMN` | `BACKFILL-REQUIRED` — seed from current `MAX_IMAGE_SIZE` so behaviour is unchanged on day one | Must be validated against the server ceiling on write |
| `max_video_bytes` | `NEW COLUMN` | `BACKFILL-REQUIRED` — seed from `MAX_VIDEO_SIZE` | same |
| `max_video_duration_seconds` | `NEW COLUMN` | `BACKFILL-REQUIRED` — seed as NULL/unlimited to avoid retroactively invalidating existing media | Enabling a real limit is a **product** decision, not a migration default |
| `max_image_pixels`, `max_image_dimension_px` | `NEW COLUMN` ×2 | `BACKFILL-REQUIRED` — seed from current constants | |
| `base_storage_bytes` | `NEW COLUMN` (`BigInteger`) | `BACKFILL-REQUIRED` — per-plan values are a commercial decision | **Do not choose numbers during audit** |
| Experience entitlements | `NEW COLUMN` ×3 booleans | `BACKFILL-REQUIRED` — set all `true` on existing plans so no current customer loses a capability | Critical: a `false` default would silently strip entitlements |
| Plan lifecycle state | `NEW COLUMN` + `ENUM-STATUS CHANGE` | `BACKFILL-REQUIRED` — map `is_active=True → ACTIVE`, `False → DISABLED_FOR_NEW_PURCHASE` | Keep `is_active` during transition; do not drop it in the same release |
| Plan version / revision | `NEW COLUMN` (+ possibly `NEW TABLE` for revisions) | `BACKFILL-REQUIRED` — version 1 for all | Decide "duplicate-to-edit" vs "immutable revisions" before migrating |
| Vendor capability flags | `NEW COLUMN`(s) | `BACKFILL-REQUIRED` | Depends on the capability list, which is not yet specified |
| Storage add-on entitlement | `NEW LEDGER TYPE` + `NEW COLUMN` (`AddonCatalog.storage_bytes_delta`, `BigInteger`) + **`ENTITLEMENT_TYPES` extension** + **amend `ck_addon_catalog_type`** | `ADDON CATALOG SEED` required | Must also widen `EntitlementTransaction.delta_value` to `BigInteger` (ANM-37). Bundles naturally with the P0-2 fix |
| Media usage accounting | `NEW TABLE` (`media_object`) + `NEW COLUMN` (`User.storage_bytes_used`, `BigInteger`) | **`BACKFILL-REQUIRED`, and it is the hard one** | Backfill must walk the filesystem and reconcile with `ProjectPair`, because `image_size`/`video_size` are unreliable (ANM-36). Needs a dry-run reporting mode before any write |
| Grandfathering representation | **Prefer NO SCHEMA CHANGE** | — | Derive from existing facts (`Project.playback_mode`, actual media sizes, actual pair counts) compared against current effective entitlement, exactly as `project_capacity_summary().over_capacity` already does. Persist only if a rule proves underivable |
| `OVER_STORAGE` / `OVER_PAIR_LIMIT` | **NO SCHEMA CHANGE** | — | Computed, following the `over_capacity` precedent |
| Admin grants into the ledger | `NEW LEDGER TYPE` (source type) | `BACKFILL-REQUIRED` (optional) — historical direct grants cannot be reconstructed; document the discontinuity | Uses the existing unused `expires_at` |
| Ownership transfer surface | `NO SCHEMA CHANGE` | — | Tables and states already exist; add a reason/detail field only if `PENDING_CAPACITY` must distinguish causes |
| `UploadSession` FK cascade (P0-5) | `SCHEMA-CHANGE-REQUIRED` | — | Independent of the commercial work; ship with the P0 batch |
| `ck_addon_catalog_type` (P0-2) | `MIGRATION-REQUIRED` | — | Ship with the P0 batch |
| Plan seed `max_pairs_per_project` (ANM-58) | `PLAN SEED UPDATE` | — | |
| Add-on catalogue (P0-3) | `ADDON CATALOG SEED` | — | Values are a commercial decision |

**Every one of the above requires a `MIGRATION TEST` and a `ROLLBACK TEST`, executed against a real
PostgreSQL database, not SQLite** — the `ck_addon_catalog_type` defect exists precisely because that
lane does not exist today.

**Sequencing note:** the media-usage backfill is the only item that touches production data at
scale, and it depends on P0-4 and P0-6 being fixed first (otherwise it would be reconciling against
a filesystem that the application is still silently orphaning into).

---

## 32. Test Gap Matrix

### Inventory

**1489 tests collected, 0 collection errors.** Directories under `tests/`:

| Directory | Coverage |
| --- | --- |
| `tests/integration/` | Largest group. Admin panel, payments, refunds (`test_admin_payment_refunds.py`), webhooks, ownership foundation (`test_domain_ownership_foundation.py`), capacity/coverage/reporting (`test_domain_commercial_capacity_and_reporting.py`), addons/entitlements, upload flows, project lifecycle |
| `tests/security/` | Security baselines — CSRF, headers, auth, secret handling, rate-limit unit behaviour |
| `tests/contracts/` | API and compatibility contracts (resumable upload, fallback analytics) |
| `tests/migrations/` | Migration mechanics |
| `tests/models/` | Model-level validation and constraints |
| `tests/gate_e` … `gate_jr` | Historical hardening gates. `gate_jr` holds the newest V1.1 work: `test_v11_admin_refund_ux.py`, `test_v11_commercial_ownership_ux.py`, `test_scanner_robustness.py` |
| `tests/compatibility/` | Legacy-shape compatibility |
| `tests/performance/` | Benchmark-style, marked `slow` |
| `tests/ops/` | Operational scripts and CLI behaviour |
| `tests/unit/` | Isolated helpers |

`pytest.ini` registers five markers (`slow`, `cv`, `security`, `contract`, `scanner_robustness`)
and sets `xfail_strict = true` — good discipline.

### The systemic gap: the test database is SQLite, always

`tests/conftest.py` builds every app fixture against SQLite, and `run-tests.ps1:23-26` actively
**refuses to run** if `DATABASE_URL` points anywhere else. Meanwhile `app.py:123-128` rejects
anything but PostgreSQL in production. **The schema and engine under test are not the schema and
engine that ship.** Three findings in this audit exist solely because of that gap:

* **P0-2** — `ck_addon_catalog_type` is in the migration and not in the model; tests use
  `db.create_all()` and never see it.
* **P0-5** — `UploadSession` FK orphan; SQLite does not enforce foreign keys by default.
* **Pair-quota concurrency** — `SELECT … FOR UPDATE` is skipped on SQLite
  (`_supports_row_level_locking`, `app.py:2571`), so the row-locking path has never executed in CI.

**This is the single highest-value test investment available**, and it is a prerequisite for every
migration in Section 31.

### V1.1 commercial behaviour coverage

| Locked behaviour | Coverage | Classification |
| --- | --- | --- |
| Plan family Individual / Vendor | vendor *identity/UX* covered in `test_v11_commercial_ownership_ux.py`; **plan family itself does not exist** | `TEST-GAP` (blocked on schema) |
| `max_pairs_per_project` enforcement | partial — route-level rejection tested; **no concurrency test**, no post-downgrade test | `TEST-GAP` |
| Per-plan max image / video bytes | none — limits are global constants | `TEST-GAP` |
| Video duration limit | validation helper has coverage; the limit is disabled by default so no end-to-end enforcement test exists | `TEST-GAP` |
| Image pixel / dimension limit | validator covered | `PASS` |
| Per-account storage allowance | none — concept does not exist | `TEST-GAP` |
| Storage add-on | none | `TEST-GAP` |
| **Upgrade behaviour** | **none anywhere** — no test asserts what happens to `projects_used`, `scans_used`, or purchased entitlements on activation. This is why P0-1 survived | `TEST-GAP` (highest priority) |
| **Downgrade behaviour** | none — no downgrade flow exists | `TEST-GAP` |
| `OVER_STORAGE` / `OVER_PAIR_LIMIT` | none | `TEST-GAP` |
| `OVER_PROJECT_CAPACITY` | partial — `project_capacity_summary` has coverage | `PARTIAL` |
| Grandfathering | none — no representation | `TEST-GAP` |
| Pair replacement | file-swap path covered; **no test that replacement leaves the pair count unchanged**, none for plan-policy revalidation | `PARTIAL` |
| Project transfer + storage | transfer domain helpers covered in `test_domain_ownership_foundation.py`; **no route tests, because there are no routes**; no storage dimension | `PARTIAL` |
| Playback-mode entitlement | per-project persistence covered; entitlement does not exist | `TEST-GAP` |
| Admin plan configuration | add/edit routes covered for existing fields | `PARTIAL` |
| **Capacity/storage/pair race conditions** | none — impossible to test meaningfully on SQLite | `TEST-GAP` |
| **Rate limiting under multiple workers** | none — the limiter is per-process and tests are single-process | `TEST-GAP` |
| Migration up/down against PostgreSQL | none | `TEST-GAP` |
| Refund flows | strong — `test_admin_payment_refunds.py` + `test_v11_admin_refund_ux.py` | `PASS` |
| Webhook idempotency/replay | strong | `PASS` |
| Moderation | strong | `PASS` |
| Scanner robustness | strong, dedicated marker | `PASS` |

### Full regression

**Authoritative command** (the repo's own `run-tests.ps1 -Suite full`, but that script hard-codes
`$ExpectedRoot = "F:\ScanStory-main\ScanStory-main"` and uses bare `python`, so it cannot run from
this worktree — P1-36). The equivalent, using the authoritative venv:

```
cd F:\ScanStory-main\ScanStory-v1.1-agent1
$env:SCANSTORY_TESTING="1"; $env:FLASK_SECRET_KEY="<any>"; $env:DATABASE_URL=""
$env:RAZORPAY_KEY_ID=""; $env:RAZORPAY_KEY_SECRET=""
$env:RECAPTCHA_SITE_KEY=""; $env:RECAPTCHA_SECRET_KEY=""
F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest -q
```

Prerequisites: the authoritative venv; `SCANSTORY_TESTING=1`; `DATABASE_URL` unset or SQLite
(startup refuses otherwise); Razorpay/reCAPTCHA keys blank so the clients initialise to `None`. No
Redis, SMTP, or PostgreSQL is required — `tests/conftest.py:69-83` monkeypatches SMTP and
`requests.Session.request` to raise on any unmocked external call.

**Executed during this audit. Result: `1 failed, 1487 passed, 1 skipped, 4591 warnings in 2562.56s`
(42m43s).** The single failure is
`tests/security/test_security_health_performance.py::test_admin_media_uses_private_cache_not_public`,
which asserts `200` and receives `404` for `/admin/image`, `/admin/video`, and `/admin/qr` — a real
defect on the baseline, recorded as **P0-9**, not a flaky or environment-dependent failure. The
skip is a Playwright-dependent browser crop test. The 4591 warnings are dominated by the
SQLAlchemy `Query.get()` legacy warnings and the Flask-Migrate `get_engine` deprecation noted in
Section 22 and Section 23.

This result is itself an audit finding: **the baseline is not green on the full suite**, and the
focused gate cited in the brief does not include the test that fails. Runtime is dominated by the `cv`-marked scanner tests in the first ~10% of the run; the
`-m "not slow and not cv"` subset is the practical fast lane.

### Recommended additions, in priority order

1. A **PostgreSQL test lane** running migrations (`upgrade head`) rather than `create_all()`. It
   would have caught P0-2 and P0-5, and it unblocks every Section 31 migration test.
2. **Upgrade/downgrade entitlement tests** asserting that `projects_used`, `scans_used`, and
   purchased ledger entitlements survive a plan change. Would have caught P0-1.
3. **Concurrency tests** on PostgreSQL for project quota, pair quota, launch capacity, and add-on
   fulfilment.
4. **Delete-path tests** asserting that every expected file is gone after project deletion, for
   both user-owned and admin-owned projects. Would have caught P0-4.
5. A **model-vs-migration drift check** — reflect the migrated schema and diff it against the model
   metadata.

---

## 33. Server Team Questionnaire

Every item is `SERVER-TEAM-VERIFY`. **Never request passwords, API secrets, SMTP passwords, or DB
passwords.** All evidence must be redacted before sharing.

### A. Host and platform (1-7)

| # | Question | Why | Evidence required |
| --- | --- | --- | --- |
| 1 | What exactly is allocated on the Hostinger KVM8 instance, and is anything else co-tenanted on it? | Sizing every conclusion below; co-tenancy invalidates CPU/RAM headroom assumptions | Plan name + `nproc`, `free -h`, `df -h`, and the list of other services on the host |
| 2 | Which Linux distribution and version? | Determines Python availability, systemd version, OpenCV package provenance | `cat /etc/os-release`, `uname -a` |
| 3 | How many CPU cores are available to the app? | Gunicorn worker sizing; `MAX_WORKERS = min(8, os.cpu_count())` (`app.py:3570`) sizes the app's own thread pool from this | `nproc`, `lscpu` |
| 4 | How much RAM, and what is current headroom under load? | Video/image processing is memory-heavy; workers × pool × OpenCV | `free -h`, peak from monitoring |
| 5 | Total disk, current usage, and growth rate of the media directory? | There is **no storage accounting and no orphan cleanup** — growth is currently unbounded and invisible (Section 9) | `df -h`, `du -sh` of the data dirs, 30-day trend |
| 6 | What filesystem and mount options back the media path? Is it a separate volume? | `os.replace()` atomicity requires same-filesystem; `os.path.getsize` correctness | `mount`, `findmnt`, `lsblk -f` |
| 7 | What are the absolute values of `SCANSTORY_DATA_DIR` and `SCANSTORY_ADMIN_DATA_DIR`, and do they survive a redeploy? | The code default is the **relative** `"data"` (ANM-53); a redeploy that recreates the working directory would strand all media | The two paths, plus proof they are outside the deploy artifact |

### B. Python and application runtime (8-15)

| # | Question | Why | Evidence required |
| --- | --- | --- | --- |
| 8 | Which Python version runs the app? | OpenCV/NumPy/SQLAlchemy compatibility | `python --version` from the venv |
| 9 | Where is the venv, who owns it, and was it built from `requirements.txt` at the release commit? | Reproducibility; `docs/production/deployment-runbook.md` requires it | `pip freeze` diffed against `requirements.txt` |
| 10 | Is Gunicorn the WSGI server, and at what version? | **No Gunicorn config exists in the repository** | `gunicorn --version`, the full command line |
| 11 | How many Gunicorn workers? | Multiplies every rate limit by N (P0-8) and multiplies DB connections by 30 (`app.py:175-194`) | The `-w` value from the redacted unit file |
| 12 | Which worker class (sync / gthread / gevent)? | The app uses a `ThreadPoolExecutor` and blocking SMTP; an async class would misbehave | The `-k` value |
| 13 | How many threads per worker? | Combined with pool size determines real DB concurrency | The `--threads` value |
| 14 | What are `--timeout` and `--graceful-timeout`? | A 1 GiB upload must not be killed mid-request; video processing is slow | Both values |
| 15 | What is the restart policy, and does a restart drain in-flight uploads? | Restarts wipe all rate-limit counters (P0-8) and can abort resumable sessions | `Restart=` and `KillMode=` from the redacted unit |

### C. Reverse proxy and TLS (16-23)

| # | Question | Why | Evidence required |
| --- | --- | --- | --- |
| 16 | Is Nginx the reverse proxy, and at what version? | **No Nginx config exists in the repository** | `nginx -v` |
| 17 | **What is `client_max_body_size`, globally and for `/upload` and `/api/uploads/sessions/*/chunk`?** | **P0-7.** `MAX_CONTENT_LENGTH` is unset, so this is the *only* body bound. The chunk route needs ≥1 MiB and should not greatly exceed it (`docs/production/README.md:76`) | The redacted `server`/`location` blocks |
| 18 | What are `proxy_read_timeout`, `proxy_send_timeout`, `proxy_connect_timeout`, and `client_body_timeout`? | A slow 1 GiB upload or a long video-processing request must not be cut | The directive values |
| 19 | Does Nginx serve `/static` and media directly, or does everything proxy to Flask? | Flask currently serves video via `send_from_directory` with Range support; large-file serving through Python is inefficient | The `location` blocks |
| 20 | Is TLS terminated at the proxy, with which certificate authority and renewal mechanism? | `SESSION_COOKIE_SECURE` and HSTS depend on it | `openssl s_client` summary, renewal timer status |
| 21 | Which domains and DNS records point at this host? | The reCAPTCHA hostname allowlist (`app.py:455`) and CSP must match | DNS records, `server_name` |
| 22 | Are `SECURITY_HSTS_ENABLED`, `SECURITY_CSP_ENABLED`, `SECURITY_CSP_ENFORCE` set, and to what? | CSP ships report-only by default (P2-08) | `curl -I` response headers |
| 23 | **Does the proxy overwrite `X-Forwarded-For` and `X-Forwarded-Proto`, and is the app port unreachable from the internet?** | `ProxyFix(x_for=1)` (`app.py:92`) trusts exactly one hop; a spoofable header defeats every per-IP limit | The redacted `proxy_set_header` block plus firewall rules proving the app port is closed |

### D. PostgreSQL (24-28)

| # | Question | Why | Evidence required |
| --- | --- | --- | --- |
| 24 | PostgreSQL major version, and is it on this host or managed? | `SELECT … FOR UPDATE` and FK enforcement are load-bearing (Sections 10, 22) | `SELECT version();` |
| 25 | What are `max_connections`, `shared_buffers`, `work_mem`, and `statement_timeout`? | The app opens up to 30 connections **per worker** | `SHOW` output for each |
| 26 | Given the worker count from Q11, does `workers × 30` fit inside `max_connections` with headroom? | Pool exhaustion presents as intermittent 500s | The arithmetic, confirmed |
| 27 | Where does the database store its data, on which volume, and how large is it? | Capacity planning; restore sizing | `SHOW data_directory;`, `df -h` for that mount |
| 28 | How is the database backed up — tool, frequency, destination, encryption, and last successful run? | **No backup tooling exists in the repository** (Section 28) | Backup job status, last-success timestamp, redacted destination |

### E. Redis and workers (29-33)

| # | Question | Why | Evidence required |
| --- | --- | --- | --- |
| 29 | Is Redis reachable from the app host, at which version, and is it exposed beyond localhost? | `REDIS_URL` has no default; the app fails closed in production (`app.py:133-140`) | `redis-cli PING`, `INFO server`, bind address |
| 30 | What is Redis's persistence and `maxmemory-policy`? | An eviction policy that drops keys would silently lose queued jobs | `CONFIG GET maxmemory-policy`, `CONFIG GET save` |
| 31 | Is there a supervised RQ worker service, how many, and on which queue name? | **Redis up ≠ a worker consuming** — `/ready` cannot tell the difference (P0-6, P1-07). `RQ_QUEUE_NAME` default is `scanstory-processing` | `systemctl status` for the worker unit, `rq info` |
| 32 | How are workers supervised and restarted, and does RQ's **scheduler** run? | Delayed `Retry` intervals require it; `rq_worker.py:29` sets `with_scheduler=False` (P1-24) | Redacted unit file; confirmation of scheduler presence |
| 33 | What happens to in-flight jobs on worker restart or deploy? | `recover-processing-jobs` exists but is **never scheduled** (P1-22) | The documented behaviour plus any recovery automation |

### F. Mail and payments (34-36)

| # | Question | Why | Evidence required |
| --- | --- | --- | --- |
| 34 | Is the SMTP host reachable from the app host, on which port, and is outbound SMTP firewalled? | Mail is sent **synchronously in the request path** with no retry (P1-25) | Connectivity test result, no credentials |
| 35 | Has a real end-to-end send been performed in production/staging, and what is `SMTP_SECURITY` set to? | `none` is permitted with no guard (P1-26); no last-success state is tracked (Section 25) | A delivered test-message header trail with the credential redacted |
| 36 | Is the Razorpay webhook endpoint publicly reachable over HTTPS, and has a real test-mode delivery been received? | `docs/production/README.md:125-130` explicitly records this as **not yet staging-certified** | Razorpay dashboard delivery log plus the app's `razorpay_webhook_events` row |

### G. Operations, monitoring, and security (37-45)

| # | Question | Why | Evidence required |
| --- | --- | --- | --- |
| 37 | What firewall rules are in force, and is the app port reachable only from the proxy? | Bypassing the proxy bypasses `ProxyFix` and every per-IP limit | `ufw status` / `iptables -L` |
| 38 | Which supervisor manages the app and workers? | Restart, log, and rollback behaviour all derive from it | `systemctl list-units` for the relevant units |
| 39 | What infrastructure monitoring exists (CPU, RAM, disk, process liveness)? | | Dashboard/screenshot or config |
| 40 | **Is there any application error/exception monitoring?** | **`MISSING` in code** — no Sentry/OTel anywhere (P1-06). If the host has none either, production is fully blind | Tool name and a sample captured event |
| 41 | Where do application logs go, and are they retained? | Logging is stderr-only at DEBUG with no rotation (P1-03) | Log destination, sample redacted lines |
| 42 | Is log rotation configured, with what size/age limits? | Unbounded DEBUG logs will fill the disk | `logrotate` config or journald settings |
| 43 | Are there disk-space alerts, and at what thresholds? | Media growth is unbounded and unaccounted (Section 9) | Alert rule definitions |
| 44 | Are there CPU/RAM alerts, and at what thresholds? | | Alert rule definitions |
| 45 | Are there backup-failure alerts? | A silent backup failure is indistinguishable from success today | Alert rule definitions |

### H. Backup, restore, and recovery (46-52)

| # | Question | Why | Evidence required |
| --- | --- | --- | --- |
| 46 | Is the **media directory** backed up, separately from the database? | `backup-restore-runbook.md:6-13` requires it; no tooling exists in the repo | Backup job definition covering the data dirs |
| 47 | Are backups stored **off-server**, encrypted, and where? | | Redacted destination and encryption method |
| 48 | What is the backup retention schedule? | The repo explicitly defers this to business policy and declines to invent one | The policy, once decided |
| 49 | **Has a restore rehearsal actually been executed, and when?** | The repo can only prove the *procedure* exists, never that it has been run | Rehearsal date, duration, and outcome |
| 50 | What is the agreed RPO? | Determines acceptable data loss | The number, with business signoff |
| 51 | What is the agreed RTO? | Determines acceptable downtime | The number, with business signoff |
| 52 | Is time synchronised (NTP/chrony)? | Every expiry, coverage window, reservation TTL, and OTP lifetime depends on clock accuracy; `get_utc_now()` uses naive UTC throughout | `timedatectl status` |

### I. Environments, deployment, and readiness (53-60)

| # | Question | Why | Evidence required |
| --- | --- | --- | --- |
| 53 | Does a staging environment exist that mirrors production, including PostgreSQL, Redis, and a worker? | Nothing in Sections 34-35 can be certified without one | Staging inventory |
| 54 | What is the deployment process, and is it scripted or manual? | `deployment-runbook.md` defines 30 steps; are they automated? | The deploy script or runbook execution record |
| 55 | How are secrets stored and injected, and who can read them? | `FLASK_SECRET_KEY`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, SMTP credentials | The mechanism only — **never the values** |
| 56 | What is the rollback process, and has it been exercised? | `rollback-runbook.md` forbids `downgrade base`; M-03 makes one migration one-way | Rollback record |
| 57 | Who holds rollback authority? | `docs/production/README.md:116` leaves it as a placeholder | The named role |
| 58 | **Which exact environment variables are set in production?** Specifically: `SCANSTORY_QUEUE_MODE`, `REDIS_URL` (presence), `FLASK_ENV`/`APP_ENV`/`SCANSTORY_PRODUCTION`, `SCANSTORY_TESTING`, `MAX_CONTENT_LENGTH`, `MAX_VIDEO_DURATION_SECONDS`, `SESSION_COOKIE_SECURE` | **P0-6 and P0-7 are decided entirely by this answer.** A missing production flag silently enables `fake` queue mode and SQLite | The variable **names and non-secret values** from the redacted unit/env file |
| 59 | Can Redis be used for a shared rate limiter, and is `RATE_LIMIT_REDIS_URL` provisionable? | Required to fix P0-8 for multi-worker deployment | Confirmation plus any capacity constraint |
| 60 | Is there an environment where load testing may be run safely, and when? | Section 34 cannot proceed without it; **never load-test production** | Environment identity and an agreed window |

---

## 34. Load / Concurrency Certification Plan

> **CURRENT SAFE CONCURRENCY = UNPROVEN.**
>
> No load test has been executed. No concurrency number may be quoted, published, or promised until
> the measurements below are taken on production-equivalent infrastructure. This audit ran on a
> single-process Windows worktree with SQLite and no Redis; nothing here supports any concurrency
> claim whatsoever.

### Three numbers that must never be conflated

| Concept | What it means | Current value |
| --- | --- | --- |
| **Registered users** | Rows in `users` | Unbounded by design |
| **Paid-account business capacity** | `CapacityConfig.configured_limit`, default **25** (`app.py:2711`) — a commercial admission gate on how many paid accounts may exist | 25 |
| **Concurrent users** | Simultaneous in-flight requests the system can serve within SLO | **UNPROVEN** |

`CapacityConfig` is **not** a concurrency control. Its own docstring (`models.py:445-457`) describes
it as a paid-account slot counter. Treating 25 as a concurrency figure would be a category error.

### Scenarios to design (do **not** run against production — Q60)

1. Login + dashboard (authenticated read, session + DB)
2. Public QR landing → project availability resolution (the 13-surface coverage check)
3. Scanner metadata / `detect_init` (CPU-bound, feature loading, LRU cache)
4. `detect_track` sustained (the highest-rate endpoint: 240/60 s per IP)
5. Video delivery with Range requests (I/O + proxy behaviour)
6. Project creation (quota reservation contention)
7. Image upload (multipart, disk write)
8. Video upload (large body, the P0-7 path)
9. **Simultaneous multi-user uploads** (disk I/O and temp-space contention)
10. Payment order creation (external API latency in the request path)
11. Report endpoint (rate-limiter behaviour across workers)
12. Admin browsing (heavy aggregate queries)
13. **Mixed workload** approximating real traffic — the only scenario that produces a defensible number

### Staged ramp

10 → 25 → 50 → 100 → 200 → 500 concurrent users. **Advance only if the previous stage held
steady-state.** Stop on any of: error rate > 1%, p95 beyond the agreed SLO, DB connections at
`max_connections`, RQ queue depth growing monotonically, or disk usage growing without bound.

### Metrics to capture at every stage

p50 / p95 / p99 latency; error rate by status code; CPU and RAM per process; **active DB connections
vs `max_connections`**; DB query latency; Redis latency; **RQ queue depth and oldest-job age**;
worker processing throughput; disk I/O and free space; network throughput; upload completion rate;
video startup/buffering time; scanner-viewer end-to-end latency.

### Known bottleneck hypotheses to test first

These are derived from this audit and should shape the test design rather than be discovered by it:

1. **DB connection exhaustion** — 30 connections per worker (`app.py:175-194`) × worker count vs
   `max_connections`. Most likely first failure.
2. **Disk I/O and temp space under simultaneous large uploads** — every upload spools fully to disk
   before validation (`upload_validation.py:77`).
3. **Synchronous SMTP in the request path** (P1-25) — a slow mail server stalls request threads at
   registration and payment.
4. **CPU saturation from OpenCV feature extraction** — bounded by `MAX_WORKERS = min(8, cores)`.
5. **Rate limiter behaving as `N × limit`** (P0-8) — must be measured, not assumed.
6. **`load_features` LRU cache** being flushed process-wide by any project edit (`app.py:5646`).

---

## 35. Browser / Device / Network Certification Plan

### Already evidenced

`gate-jr/` in the repository contains a prior physical-device certification pass with per-file
results for Android Chrome, iPhone Safari, camera permissions, physical markers, video playback,
target loss/reacquisition, orientation lifecycle, slow network, fallback, and a supported-device
policy. **That evidence predates the V1.1 scanner and creator changes on this branch** (the merged
`agent/v1.1-experience-ux` work touched `templates/user/scanner.html`, `project_preview.html`, and
`user_create_project.html`), so it must be re-run, not inherited.

### Required matrix

| Surface | Browsers |
| --- | --- |
| Creator (desktop) | Chrome, Edge, Brave, Firefox |
| Creator (mobile) | Android Chrome, iOS Safari |
| Viewer / scanner | Android Chrome, iOS Safari — **on physical devices**; camera and WebGL behaviour cannot be certified in an emulator |

Per playback mode, all three must be exercised end to end: `tracked_overlay` (including Reacquiring
and prolonged-loss → Watch Video with the **same matched video**), `detect_once` (recognition →
uninterrupted playback; camera failure → Watch Video with the same matched video), and `direct_qr`
(direct playback, **no camera requested at all**).

### Creator upload network matrix

Throttle to **10, 5, 3.5, 3, 2, 1, 0.5 Mbps** and additionally test: added latency, packet loss,
mid-upload disconnect, **resume after disconnect**, page refresh mid-upload, browser backgrounding,
network switch (Wi-Fi ↔ cellular), and **duplicate/retried finalize**. The resumable protocol is
built for exactly these cases (Section 14) and this is the matrix that proves it. Note the
interaction with P0-7: the proxy body cap and `proxy_read_timeout` (Q17, Q18) directly determine
whether a 0.5 Mbps 1 GiB upload can ever complete.

### Viewer/download network testing (separate exercise)

Recognition load time, media buffering, cold cache, warm cache — the public media cache is
`public, max-age=3600` (`docs/production/README.md:99`), so warm-cache behaviour differs materially
and a suspended project's media may persist in a browser cache for up to an hour.

### Accessibility and layout checks

No lens scrolling; the in-lens fallback controls (`Retry Camera`, `Watch video instead`,
`Continue Scanning`) reachable without scrolling on a 375×667 viewport — a regression this codebase
has already fixed once and documented (`scanner.html:40-42`); `aria-live` regions announced;
`Permissions-Policy: camera=(self)` honoured; keyboard operability of all fallback controls.

**Status: `UNTESTED` for V1.1.** All of the above is plan-only in this audit.

---

## 36. Exact Production Certification Sequence

Ordered. Each step's exit criterion is the next step's entry criterion.

| # | Step | Exit criterion |
| --- | --- | --- |
| 1 | **Full regression on the current baseline** (Section 32 command) | **Executed in this audit: 1 failed, 1487 passed, 1 skipped.** The single failure is P0-9 and must be closed in step 2, not waived |
| 2 | **Fix the nine P0 blockers** (Section 3), each with a regression test | All nine closed; the full suite is green; new tests fail without the fix |
| 3 | **Stand up the PostgreSQL test lane** running `alembic upgrade head` instead of `create_all()` | P0-2 and P0-5 provably caught by CI |
| 4 | **Commercial entitlement implementation** — Section 37 checkpoints C1-C6, in order | Each checkpoint independently green |
| 5 | **Admin operations implementation** — Section 37 checkpoints A1-A4 | Each checkpoint independently green |
| 6 | **Write the migrations** from Section 31, each with an up test **and** a down test | All migrations reversible; single head preserved |
| 7 | **Focused test suites** per area (entitlements, storage, upgrade/downgrade, transfer, admin) | All green |
| 8 | **Full regression again**, PostgreSQL lane included | Zero failures; no regression against step 1's count |
| 9 | **Fresh PostgreSQL migration rehearsal** — empty DB → `upgrade head` → seed → smoke | Clean head, correct schema, app boots |
| 10 | **V1 → V1.1 migration rehearsal** on a restored copy of production-shaped data | All migrations apply; the media-usage backfill dry-run reports sane totals; rollback tested |
| 11 | **Bootstrap and seeding** — plans (with `max_pairs_per_project` set), add-on catalogue, admin | `/api/addons/catalog` returns real rows; plan limits explicit |
| 12 | **SMTP certification** — real send on staging, all five mail types | Delivery confirmed; failure path verified |
| 13 | **Razorpay certification** — order, browser verify, **real test-mode webhook over public HTTPS**, replay | `docs/production/razorpay-certification.md` fully satisfied, closing the known gap at `README.md:125-130` |
| 14 | **Refund certification** — subscription and add-on, including reversal and manual-review queue | Every state reachable and correctly surfaced |
| 15 | **Creator QA** — full matrix, all three experience/playback combinations | No blocking defects |
| 16 | **Viewer QA on physical devices** (Section 35) | All three playback contracts honoured, including same-matched-video fallback |
| 17 | **Network matrix** (Section 35) including resume and duplicate finalize | Uploads complete at 0.5 Mbps; resume works |
| 18 | **Moderation and Admin QA** — permissions, destructive actions, audit trail | RBAC matrix verified per role |
| 19 | **Privacy review** — consent capture, retention decisions signed off | Business/legal signoff recorded (Section 27) |
| 20 | **Server verification** — all 60 questionnaire groups answered with evidence (Section 33) | No `SERVER-TEAM-VERIFY` item outstanding |
| 21 | **Backup and restore rehearsal** — real backup, real restore, media included | Restore verified against a media manifest |
| 22 | **Security review** — rate limiting on Redis, headers, CSP enforce decision, secret handling | P0-8 closed; proxy checklist satisfied |
| 23 | **Load test on staging** (Section 34) | A measured, defensible concurrency number replaces `UNPROVEN` |
| 24 | **Staging end-to-end** — full user journey on production-equivalent infrastructure | Clean run |
| 25 | **Feature freeze** | No non-blocker merges |
| 26 | **Tag `v1.1.0-rc1`** | Tag on a green full regression |
| 27 | **Production deploy** per `deployment-runbook.md` | All 30 steps executed, backups verified first |
| 28 | **Production smoke** — `/healthz`, `/ready`, login, create, scan, pay, refund | All green |
| 29 | **Signoff** — named owners per area | Recorded |
| 30 | **Final tag and documentation update** — including correcting the stale `docs/production/` claims (P1-37) | `v1.1.0` tagged; docs match reality |

---

## 37. Final Recommended Implementation Checkpoints

Smallest safe phases. **None of these was implemented during this audit.** Each is independently
shippable and independently testable; the ordering encodes real dependencies.

### Phase 0 — Blockers (no schema change except two)

| ID | Checkpoint | Notes |
| --- | --- | --- |
| B1 | Fix plan activation: add `reconciled_scan_limit()`, stop resetting `projects_used`/`scans_used`, chain remaining validity, fix the `*30` month math | P0-1, ANM-41, ANM-42. **Do this first** — it is a live commercial-integrity bug |
| B2 | Amend `ck_addon_catalog_type` (migration) and add the matching model-level constraint | P0-2. `MIGRATION-REQUIRED` |
| B3 | Seed `AddonCatalog` + add the superadmin CRUD page | P0-3. Values are a commercial decision |
| B4 | Fix `_delete_project_files_and_rows`: use the admin-aware directory helper, log unlink failures, include `_fast.mp4` variants | P0-4, ANM-06 |
| B5 | Clear/cascade `UploadSession` FKs on project delete | P0-5. `SCHEMA-CHANGE-REQUIRED` |
| B6 | Fail closed on queue mode; make `/ready` mode-aware; add `SCANSTORY_TESTING` to the production prohibition | P0-6, ANM-52 |
| B7 | Set `MAX_CONTENT_LENGTH` and/or evidence the proxy body cap | P0-7 — resolve **with** Q17 |
| B8 | Rate-limit `/admin/login` and `/admin/forgot-password`; move the limiter to Redis | P0-8. The Redis move is the larger piece and may be split |
| B9 | Add an `owner_admin_id` branch to `project_public_access_state` so admin-owned projects resolve coverage | P0-9. **Closes the one currently-failing test.** Pairs naturally with B4, which shares the same admin-path root cause |

### Phase C — Commercial entitlement (strictly ordered)

| ID | Checkpoint | Depends on |
| --- | --- | --- |
| C1 | **`get_effective_entitlements(user)`** — a read-only resolver over *existing* data, changing no behaviour, plus tests. Feeds the materialised columns; does not replace them | B1 |
| C2 | Route every existing limit check through C1; route admin grants through the ledger using the unused `expires_at` | C1. ANM-17 |
| C3 | Add Tier-2 plan columns (`plan_family`, media limits, `base_storage_bytes`, experience flags, lifecycle) with behaviour-preserving backfill; extend the Admin plan editor; inject limits into the creator UI | C2. Section 31 |
| C4 | Enforce plan-scoped media policy at the four upload call sites and on replacement | C3. Closes ANM-13 |
| C5 | **Storage: `media_object` table + `User.storage_bytes_used` + backfill (dry-run first)** | C1, B4, B6. The largest single piece |
| C6 | Storage add-on (`BigInteger` deltas), over-storage state, storage-on-transfer | C5, B2 |

### Phase A — Admin operations

| ID | Checkpoint |
| --- | --- |
| A1 | Operations console: health tile, worker count, oldest job age, media-writable, migration head |
| A2 | Refund manual-review queue (P1-29) |
| A3 | Ownership transfer/claim HTTP surface — user routes **and** admin administration (ANM-18) |
| A4 | Plan impact preview + plan lifecycle/duplicate (C3 prerequisite for the fields) |

### Phase H — Hardening (parallelisable with C and A)

Grouped P1 items that do not block the commercial work: error-message sanitisation (P1-01, P1-02),
logging configuration and rotation (P1-03, P1-04, P1-05), error monitoring (P1-06), lockout fix
(P1-08), blocked-user coverage on upload routes (P1-09), session lifetime (P1-11), JSON auth
responses (A-04, A-05), scheduling the four maintenance CLIs (P1-22), and correcting the stale
production docs (P1-37).

### Explicitly out of scope for V1.1

Object storage / CDN migration (the `storage_provider` field in C5 keeps the door open without
committing); reviving the dormant Experience/Workspace tree; `app.py` decomposition beyond the
entitlement extraction that C1 naturally produces; any change to ORB, homography, RANSAC, optical
flow, tracking geometry, camera calibration, overlay perspective math, or tracking thresholds.

---

## 38. Final Verdict

```
LOCAL CODE READINESS = CONDITIONAL PASS
V1.1 COMMERCIAL ENTITLEMENT MODEL = REQUIRES IMPLEMENTATION
ADMIN OPERATIONS UI = PARTIAL
SERVER READINESS = UNVERIFIED
CURRENT SAFE CONCURRENCY = UNPROVEN
READY TO START PRODUCTION-HARDENING IMPLEMENTATION = YES
READY FOR v1.1.0-rc1 = NO
```

**Reading of the verdict.** `CONDITIONAL PASS` rather than `FAIL` is deliberate: the nine P0 items
are bounded, individually well-understood defects with identified fixes, not architectural faults.
The foundations that are hardest to retrofit — atomic quota reservation, webhook idempotency, refund
state integrity, the resumable upload protocol, IDOR discipline, RBAC, secret handling, and the
separation of feature eligibility from service coverage — are already sound.

`READY TO START PRODUCTION-HARDENING IMPLEMENTATION = YES` because the work is now specified: nine
blockers, a bounded commercial gap with a clear schema plan, and an ordered checkpoint sequence.

`READY FOR v1.1.0-rc1 = NO` because the entire storage dimension of the locked commercial model does
not exist, the ownership-transfer requirement has no HTTP surface, no test has ever run against
PostgreSQL, and nothing about the production host has been verified.

**Recommended first move: checkpoint B9, immediately followed by B1.**

**B9 first** because the full regression is currently **red** (P0-9), and a red baseline makes every
subsequent fix unverifiable — you cannot tell a new regression from the existing one. B9 is the
smallest change in the entire plan: one ownership branch in `project_public_access_state`. It
restores a completely non-functional class of projects and turns the suite green, which is the
precondition for trusting everything after it.

**B1 immediately after**, because it is the highest-value defect in the audit: a live
commercial-integrity bug that silently destroys purchased entitlements and bypasses the capacity
gate on every paid activation. It is contained to roughly four lines plus one new reconciler
function mirroring one that already exists — and the fact that no test anywhere asserts what happens
to usage counters or purchased entitlements across a plan change is precisely why it survived this
long. Ship it with that test.
