# V1.1 Final Production Ops & Admin Investigation Report

Lane: `agent/v1.1-production-ops` (Agent 3)
Worktree: `F:\ScanStory-main\ScanStory-v1.1-agent3-production-ops`

**Headline.** Most of Part A was already built by earlier waves and verified
here rather than rebuilt. Part A's genuinely open defects were **unbounded Redis
sockets** (every dependency probe could hang forever instead of reporting the
outage it exists to detect) and a **permanently wedged upload session** after a
crash mid-finalize. Part B was **broken**, not merely incomplete: the admin
user-detail Projects card was HTML-commented out, the project page showed media
filenames as text only, and — the worst of the three — **suspending a reported
project blinded the admin who suspended it**, so the only way to review the
evidence behind a report was to re-publish the reported content first.

---

## 1. Starting integration HEAD

`e90ade376f7e0697e805472b3f91d32af5894596`
(`Merge branch 'agent/v1.1-experience-ux' into develop/scanstory-v1.1`)

## 2. Starting branch HEAD

`e90ade376f7e0697e805472b3f91d32af5894596` — identical. Verified at start with
`git status --short` (clean), `git branch --show-current`
(`agent/v1.1-production-ops`) and `git rev-parse HEAD`. No prior commits on this
branch beyond shared history.

## 3. Ending HEAD

The docs/report commit listed last in section 4. Its hash is deliberately not
quoted here: this report is *inside* that commit, so a commit cannot contain its
own hash. `git log --oneline e90ade3..HEAD` prints it; the four code/test commits
below are fixed and quoted exactly.

## 4. Commits

| Hash | Subject |
|---|---|
| `3b9d52d` | Bound every Redis socket the app depends on |
| `2dcaa78` | Recover a crashed finalize, and stop blinding the moderator |
| `9a2a5e8` | Show the admin the evidence they are judging |
| `6675a19` | Cover the production-ops and investigation scenarios |
| *(HEAD)* | Correct the proxy checklist and report the lane — contains this report, so its own hash cannot appear in it |

Split by file grouping rather than by concern: `app.py` carries several of these
changes in distinct line ranges, and non-interactive hunk staging is not
available in this environment. Each commit body enumerates its contents.

## 5. Files changed

| File | +/- | What |
|---|---|---|
| `processing_queue.py` | +32/-6 | `redis_connection()` / `redis_socket_timeout_seconds()`; three call sites converted |
| `rate_limit.py` | +18/-1 | limiter's own Redis client bounded |
| `app.py` | +221/-32 | bounded readiness probe; `finalizing` recovery sweep; `--limit` on stale-job recovery; secret-safe forgot-password log; admin media investigation authz; report payload owner/state |
| `models.py` | +6/-1 | `UploadSession` lifecycle docstring corrected |
| `templates/admin/view_user.html` | +11/-5 | Projects card restored from HTML comment |
| `templates/admin/view_project.html` | +45/-4 | image/video evidence rendered; back-to-owner link; dead scanner-test button gated |
| `templates/admin/moderation.html` | +32/-1 | owner + project-state investigation context |
| `docs/production/security-proxy-checklist.md` | +61/-5 | three false limiter claims corrected; large-upload proxy requirements added |
| `tests/integration/test_v11_production_ops_admin_investigation.py` | new, 62 tests | certification suite |
| `tests/integration/test_admin_panel_repair.py` | +18/-4 | one assertion corrected — it encoded the moderation defect |

No file under `migrations/` touched. `scanner_runtime.py` and
`static/js/scanner-runtime.js` untouched (section 37).

## 6. Production ops audit table

| Area | Current behaviour (as read, not as reported) | Production risk | Action |
|---|---|---|---|
| Rate-limit backend | `rate_limit.py` already fully Redis-backed. `RedisRateLimiter` uses a **transactional** `pipeline()` (`INCR`+`TTL`, redis-py defaults `transaction=True` → `MULTI/EXEC`), then `EXPIRE` only when `ttl < 0`. Window is not refreshed on later hits. Fail-closed documented and implemented. `build_limiter` raises rather than silently downgrading on a bad URL. | Atomicity and sharing were already correct. **Socket was unbounded.** | Verified, not rebuilt. Bounded the socket. |
| Rate-limit keys | `_rate_limit_key(scope, *parts)` = scope + client IP + parts, parts truncated to 120 chars. Identities passed through `identity_digest()` (sha256, first 32 hex). | None found. | Verified + pinned by test. |
| Limit inventory | 20 scopes: login (IP+identity), admin login (IP+identity), register, forgot-password (user+admin), resend-OTP, upload, content_report, ownership claim + lookup, 6 scanner scopes, fallback video. | Webhook route deliberately unlimited (documented, correct — Razorpay retries rotate IPs). | No thresholds changed. |
| `/healthz` | `jsonify({"status":"ok"})`, 200, `no-store`. Zero dependency calls. | None. | Verified + pinned. |
| `/ready` | `_readiness_checks()`: DB `SELECT 1`, `queue_mode()`, `redis_ready_check()`, `queue_worker_state()` (worker count + stale-heartbeat guard), plus production config/payments/CSP labels. 503 if **any** value is `"unavailable"` (scans values, not named keys). Generic exception handler → `{"database":"unavailable"}`. | **No bounded check time.** DB statement and every Redis call could hang. | Bounded both. |
| PostgreSQL dependency | `connect_timeout: 10`, `pool_timeout: 30`, `pool_pre_ping`. | Connect bounded; **statement not**. A connection that establishes then stops answering hung the probe. | `SET LOCAL statement_timeout` in the probe transaction. |
| Redis dependency | `Redis.from_url(...)` in 4 places with **no** `socket_timeout` (redis-py default `None`). | **P0.** Blackholed Redis = infinite hang in `/ready`, `queue_worker_state`, enqueue, admin operations page, and the login-path limiter. | `redis_connection()`; limiter bounded separately. |
| RQ dependency | `queue_worker_state()` returns `("ok"/"unavailable"/"not_applicable", count)`; count only, never worker names or job payloads. `queue_available()` fails closed on fake/inline in production. | Correct. | Verified. |
| Enqueue failure visibility | `enqueue_processing_job` marks the job `failed` + `QUEUE_UNAVAILABLE` **and commits** before re-raising `QueueUnavailable`. Idempotency via `active_project_job()` under `SELECT … FOR UPDATE`. `_finalize_enqueue_and_complete` leaves sessions `assembled`, not `completed`, and returns `QUEUE_ENQUEUE_FAILED` 502. | No gap found. No path claims success on a failed enqueue. | Verified. |
| Upload cleanup | `cleanup-upload-sessions`: `status=='active'` only, TTL-or-inactivity, dry-run default, `--limit` bounded, `_safe_delete_upload_temp` refuses outside `TMP_UPLOADS_DIR`. | **P0: `finalizing` was unreachable.** Crash mid-finalize → permanent `FINALIZE_IN_PROGRESS` 409 wedge. | Second sweep added. |
| Long-pause window | `UPLOAD_SESSION_ABANDONED_STALE_MINUTES` defaults to `UPLOAD_SESSION_TTL_MINUTES` (1440). Phase 2's fix is real. | None. | Verified + pinned. |
| Stale ProcessingJob | `recover-processing-jobs` exists; dry-run default; judges on `last_heartbeat_at`. | **Unbounded `.all()`.** | `--limit` (default 200). |
| Logging helpers | `_log_upload_timing` / `_log_processing_timing` are strict **allowlists** — unknown keys silently dropped. `safe_error_summary` strips paths and `secret|token|password|signature=` fragments. | Excellent. | Verified + pinned with sentinel-injection tests. |
| Secret leakage sweep | Repo-wide grep of every `logger.*`/`print` against `DATABASE_URL`, `SECRET_KEY`, `password`, `_token`, `Authorization`, `signature`, `os.environ` returned **one** hit: `print(f"❌ Forgot password email error: {e}")`. | Raw exception text in a password-reset path; the wrapped block includes OTP creation and mail send. | Replaced with `type(e).__name__`. |
| Payment/webhook logging | Already reason-coded (`razorpay_webhook_rejected reason=invalid_signature`), no payload. | None. | Verified. |
| Config guards | `_validate_required_runtime_config` covers missing/non-Postgres `DATABASE_URL`, `FLASK_SECRET_KEY`, SMTP set, `SCANSTORY_QUEUE_MODE=rq`, `REDIS_URL`, `SESSION_COOKIE_SECURE`, `SCANSTORY_DEV_TESTING=0`, `SCANSTORY_TESTING=0`, CSP flags, Razorpay keys, and refuses to boot with an undeclared environment. | Nothing missing. | Verified, not rebuilt. |
| Reverse proxy docs | Proxy checklist existed but stated the limiter *is* process-local (false since Wave 1) and had **zero** body-size/timeout/buffering guidance. | Actively misleading an operator. | Corrected + extended. |
| Backup/restore | `docs/production/backup-restore-runbook.md` covers DB + media + retention + restore drill. Five read-only `scripts/production/*.ps1`, all refusing production without an explicit confirmation switch. | None. | Verified by executing the refusal paths. No new script written. |

## 7. Existing rate-limit architecture

Already correct and shared. One module-level `limiter` in `rate_limit.py`, two
interchangeable backends behind `check(key, limit, window) -> (allowed,
retry_after)`; every one of the 20 limited endpoints routes through
`_check_rate_limit`. Wave 1's P0-8 is real, not aspirational — including its
multi-worker test (`test_wave1_p0_blockers.py:904`, two `build_limiter(client=shared)`
instances over one Redis).

Atomicity was already sound: `pipeline()` is transactional in redis-py, so
`INCR`+`TTL` execute as one `MULTI/EXEC`. The trailing `EXPIRE` is only issued
when `ttl < 0` and is self-healing — if a process dies between `INCR` and
`EXPIRE`, the next call observes `ttl < 0` and sets it. There is no race-prone
GET-then-SET anywhere. **No limiter framework was invented or replaced.**

## 8. New shared rate-limit result

No new limiter. The one real defect was that `build_limiter` constructed
`Redis.from_url(url)` with no socket timeout, so a fail-closed policy could
never actually fire against an unreachable-but-not-refusing Redis — it would
hang the login request thread instead. Now bounded by
`REDIS_SOCKET_TIMEOUT_SECONDS` (default 5), deliberately duplicated in
`rate_limit.py` rather than imported from `processing_queue` to keep that module
dependency-free as its docstring promises.

Verified: shared budget across two limiter instances on one Redis; TTL set once
and equal to `Retry-After`; window reset after expiry; key isolation; fail-closed
on outage with `1 ≤ Retry-After ≤ 5`; no stack trace in the log line; hashed
identities in keys; in-memory dev fallback intact; all five audited thresholds
byte-identical.

## 9. Redis outage policy

**Unchanged and now enforceable.** Fail closed for everything behind the
limiter — these are auth, OTP-mail and abuse-reporting paths, and the same
outage already takes `/ready` to 503, so the instance is out of rotation
regardless. Users get `429` + short `Retry-After`; the log line carries
`type(exc).__name__` only. Public pages that are not rate-limited are unaffected
by a Redis outage. The material change is that the bounded socket makes this
policy *reachable* rather than a hang. Documented in the proxy checklist.

## 10. Health result

Correct as found, now pinned. `/healthz` is pure liveness — 200,
`{"status":"ok"}`, `Cache-Control: no-store`, and a test that monkeypatches
`redis_ready_check`/`queue_worker_state` to raise proves it touches neither.
No DB query.

## 11. Readiness result

Correct as found (worker-awareness from P1-3 is real), extended with the one
missing property: **bounded check time**. `_readiness_probe_database()` issues
`SET LOCAL statement_timeout = 3000` on PostgreSQL before `SELECT 1`, scoped to
that transaction so no ordinary query changes behaviour; SQLite skips the pragma.
Redis-side calls are bounded by `redis_connection()`. 200/503 semantics kept;
response stays machine-readable (`status` + flat `checks` dict).

Known remaining edge, marked in-code with a `ponytail:` comment: SQLAlchemy's
`pool_pre_ping` issues its own unbounded `SELECT 1`. Bounding it requires a
global `statement_timeout` via `connect_args options=`, which would change every
query's behaviour — deliberately not done in a parallel hardening lane.

## 12. Database dependency result

`connect_timeout: 10` + `pool_pre_ping` + `pool_timeout: 30` already bounded
reaching the database. The probe statement is now bounded too. A failure yields
exactly `{"database": "unavailable"}` — verified against an exception whose text
embedded a sentinel connection string, with the response asserted free of both
the credential and the message.

## 13. Redis dependency result

Was the biggest Part A hole. Four `Redis.from_url` sites had no timeout;
redis-py defaults `socket_timeout=None`, and an unreachable Redis does not
always *refuse* — a firewall that DROPs, a dead host with a live ARP entry, or a
hung server all accept-then-never-answer. `Redis.ping()` then blocks forever, so
`/ready` joined the outage it exists to report.

Converted to `redis_connection()`: `_enqueue_transport`, `_rq_workers_for_queue`,
`redis_ready_check`, plus the admin operations queue summary in `app.py` (which
could hang an admin's request thread; it already degrades to a safe
`payload["error"]`).

**`rq_worker.py` deliberately left alone.** A worker's blocking dequeue holds a
long-lived connection; imposing a 5-second read timeout on it is a known footgun
that produces spurious timeouts. Its blocking behaviour is correct.

## 14. RQ / queue dependency result

Verified correct, unchanged. `queue_worker_state()` distinguishes
`not_applicable` (fake/inline — a supported mode, not a failure) from
`unavailable`, applies a stale-heartbeat cutoff
(`RQ_WORKER_STALE_AFTER_SECONDS`, default 420) on top of RQ's own registry, and
returns a count only — never hostname-pid worker names or job payloads.

## 15. Processing enqueue failure result

Audited all five enqueue paths (initial, creator repair/reprocess, resumable
finalize, multi-pair finalize, admin repair). **No gap found; nothing rewritten.**
No path reports scheduled work for a failed enqueue: the job row is committed as
`failed` with `QUEUE_UNAVAILABLE` before the exception propagates, users get a
safe code, raw Redis exceptions never surface, and idempotency holds via
`active_project_job()` under a row lock. Resumable finalize's own recovery
contract (leave sessions `assembled`, retry re-runs only the enqueue) is intact.

## 16. Upload-session cleanup result

The `active` sweep was already correct on every requirement: dry-run default,
`--limit` bounded, idempotent, `completed` never queried, temp deletion refused
outside `TMP_UPLOADS_DIR`. All re-verified.

Added a second bounded sweep for `finalizing` (section 18). Both sweeps share
`--limit` and the single terminal commit.

## 17. Long-pause cleanup compatibility

Compatible. Phase 2's fix is real:
`SCANSTORY_UPLOAD_SESSION_ABANDONED_STALE_MINUTES` defaults to
`UPLOAD_SESSION_TTL_MINUTES` (1440), so the shorter of the two inactivity
windows no longer undercuts the advertised 24-hour resume window. Pinned by a
test asserting the equality **and** that a five-hour-idle paused upload (well
past the old 120-minute default) survives `--apply`.

The proxy checklist now also warns that a shorter proxy send/keepalive timeout
silently undercuts this window regardless of app config.

## 18. Finalizing-session decision

**Implemented recovery, with per-outcome reasoning.** This was a genuine P0.

The defect: `models.py` documents `finalizing` as a state that "is never a
resting state a client should see" — true for every *handled* failure, because
`_finalize_assemble_and_validate`'s `fail()`/`fail_group()` always move the row
on. An *unhandled* death mid-finalize (deploy restart, OOM kill, proxy timeout
severing the request) broke that promise, and nothing could recover it: the
cleanup sweep queried `status=='active'` only, and the finalize gate claims only
`active`/`assembled` rows. The project was wedged behind `FINALIZE_IN_PROGRESS`
409s permanently — no client, admin or CLI path out. `cancel` was equally
blocked (valid from `active` only).

Recovery is safe because `project_id` is a precise marker of what already
committed — it is set only in the step-3 success block, in the same transaction
as the Project, ProjectPair, quota reservation and media-ledger rows:

- **`project_id` set** → `assembled`. Project, pair, quota and ledger all exist;
  only QR + enqueue + settle remained, which is exactly what `assembled` means
  and exactly what retry-finalize already recovers idempotently. No
  re-validation, no second quota unit, no duplicate project.
- **`project_id` NULL and assembled temp file present at the declared length**
  → `active`. Nothing durable was created and no quota consumed, and the bytes
  are genuinely re-finalizable, so the creator gets the transfer back rather
  than paying for a crash. Temp file preserved.
- **`project_id` NULL and no intact temp file** → `failed`,
  `failure_code=FINALIZE_INTERRUPTED`. Validation had already consumed the file,
  so any retry could only fail `STORAGE_INCONSISTENT`. Terminal and honest beats
  advertising a session with nothing left to resume. Temp file deleted.

Three independent guards against double-finalization: a generous threshold
(`SCANSTORY_UPLOAD_FINALIZING_STALE_MINUTES`, default 120 — finalize runs
synchronously in one HTTP request, so a worker or proxy timeout kills a real one
in seconds to minutes, never two hours); a conditional
`UPDATE … WHERE status='finalizing'` so a surviving finalize wins and the sweep
no-ops; and dry-run default. `failure_code` is `String(50)`, unconstrained — **no
migration**. `models.py`'s docstring corrected to match reality.

## 19. Stale ProcessingJob decision

Hardened the existing command rather than building a scheduler.
`recover-processing-jobs` already had the right shape — dry-run default,
`last_heartbeat_at`-based staleness (refreshed by `mark_job_processing` and every
`mark_job_*` transition, so a legitimately long job that is still beating is
never a candidate), retry-budget-aware `retrying` vs `failed`. Its one real
defect was an **unbounded `.all()`**; now `--limit` (default 200), reflected in
the output line. Verified: 3 stale jobs with `--limit 1 --apply` recovers exactly
one; a job idle 9 hours but heartbeating *now* yields `Stale jobs found … 0` and
stays `processing`. No new scheduler, no automatic mutation, no destructive
default.

## 20. Logging / secret-redaction result

The telemetry helpers were already the strongest pattern available — strict
key **allowlists** that silently drop anything unrecognised — and are now pinned
by tests that inject sentinel `Authorization`, `DATABASE_URL`, `password` and
`RAZORPAY_KEY_SECRET` values and assert none appear.

One real leak found and fixed, and only one: `print(f"❌ Forgot password email
error: {e}")` in `/forgot-password/`. That `except` wraps both `_create_otp()`
and the OTP mail send, so the exception text can carry the OTP code, the
recipient address or SMTP server dialogue. Now
`app.logger.warning("forgot_password_otp_dispatch_failed error=%s", type(e).__name__)`
— matching the established `razorpay_webhook_rejected reason=…` convention.
Pinned by a test that raises an exception containing a sentinel SMTP password and
a sentinel code and asserts neither reaches the log.

Health/readiness verified secret-free against six sentinel environment values
(dummy values only — no real secret in any fixture), also asserting absence of
`Traceback`, `File "`, `psycopg`, `sqlalchemy.exc`.

## 21. Operational telemetry result

No new observability platform, and no new telemetry either — the audit found the
required fields already emitted. Upload: session id, project id, set index/count,
bytes, offsets, duplicate-chunk and offset-mismatch flags, per-phase durations,
safe failure code. Processing: job id, project id, attempt count, state, safe
failure code, durations. Dependencies: `/ready`'s `checks` dict already carries
database / queue / workers / usable_worker_count. Adding fields would have been
addition for its own sake; the gap was that none of it was *pinned*, which the
new tests fix.

## 22. Production-config guards

Verified, not rebuilt — Wave 1 covered this properly. Every item on the brief's
list is caught by `_validate_required_runtime_config`: missing `DATABASE_URL`,
non-PostgreSQL `DATABASE_URL` (including an explicitly requested unsupported
driver, normalized *before* the check), missing `FLASK_SECRET_KEY` with no
insecure fallback, missing `REDIS_URL`, `SCANSTORY_QUEUE_MODE != rq`, SMTP
incompleteness, `SESSION_COOKIE_SECURE` off, `SCANSTORY_DEV_TESTING=1`,
`SCANSTORY_TESTING=1`, CSP disabled, Razorpay config missing. It also refuses to
boot when *no* environment is declared — closing the omission path that let a
deploy run queue mode `fake` with a green `/ready`. Nothing added.

## 23. Reverse-proxy requirements

No in-repo Nginx/Hostinger config exists, so none was edited — documentation
only, and flagged as such in the doc itself.

Two real defects fixed in `docs/production/security-proxy-checklist.md`:

1. **Three false statements.** The doc still told operators the limiter *is*
   process-local, that a shared limiter *is required before horizontal scale*,
   and to *treat it as interim until the Redis limiter is built*. Wave 1 built
   it; the doc was never updated. An operator following it would not set
   `RATE_LIMIT_REDIS_URL` and would silently run `N x limit`. Replaced with the
   actual contract: the variable is required in production, the fallback is
   dev/test only, outage policy is fail-closed, and
   `REDIS_SOCKET_TIMEOUT_SECONDS` must stay set.
2. **No large-upload guidance at all.** Added body size (≥ largest chunk, and
   above the largest whole pair for the multipart route), `proxy_request_buffering
   off`, read timeout exceeding worst-case finalize, send/keepalive tolerating a
   paused mobile uploader, no body rewriting, and Range pass-through — plus the
   operational follow-up that a proxy read timeout is precisely what creates a
   stuck `finalizing` session, with the cleanup command and the alert to watch.

## 24. Backup/restore readiness

Adequate; nothing built. The runbook covers PostgreSQL and media, frequency,
retention, restore order, and a post-restore verification list that includes
media decode and Range requests. The five `scripts/production/*.ps1` are
read-only and gated: `verify_required_env.ps1 -Environment production` refuses
without `-ConfirmProductionReadOnly`, and `smoke_health_ready.ps1` refuses a
non-HTTPS `BaseUrl` and a production probe without `-ConfirmProductionProbe`.
Both refusal paths executed and confirmed. Writing a "tiny helper" here would
have been unrequested addition.

## 25. Admin View User result

**Was broken; repaired minimally.** `admin_view_user` has always queried the
user's projects and passed `projects` to the template — but the **entire Projects
card in `view_user.html` was wrapped in an HTML comment**, so the investigation
chain had no link at all and the query result was silently discarded. The
commented block also contained a malformed `<thead>` (a `<tr>` closed by
`</thead>`), which is likely why it was disabled.

Restored: card uncommented, `<tr>` closed properly, `created_at` guarded against
`None`. The View button targets `admin_view_project`, already gated on
`admin.projects.view` — no new route, no new permission. User list and detail
pages themselves were already correct.

## 26. Admin user-project navigation result

Now end-to-end and bidirectional. Admin → Users → View User lists that user's
projects by name with a working link → project detail. Verified the chain loads,
that the correct project name and link appear, and — separately — that a second
user's project never appears on the first user's page (no context mixing).

Added the missing return path: the chain was one-way, with nothing linking a
project back to its owning account. `view_project.html` now renders a "Back to
Owner" button (`admin_view_user`) alongside "Back to Projects", shown only when
the owner is a user with a recorded id.

Also gated a dead control: "Open Scanner Test" was rendered unconditionally,
but `admin_scanner_test_entry` aborts 404 unless the project is owned by *that*
admin — so on any creator's project the button was always broken. Now shown only
when the route can succeed.

## 27. Admin project image/video evidence result

**Was broken in two independent ways; both repaired.**

*Rendering.* `view_project.html` displayed `pair.safe_image_filename` and
`pair.safe_video_filename` as **text only**. An admin could read a filename but
could not see what image the creator used or what video is attached to it. Now
each pair row renders an `<img>` and a `controls preload="metadata"` `<video>`
against the **existing** `serve_image`/`serve_video` routes (or the
`serve_admin_*` pair for admin-owned projects, selected by one Jinja variable).
No new endpoint. CSP already permits `'self'` for `img-src` and `media-src`, so
no policy change.

*Authorization — the serious one.* All four media routes gated solely on
`_project_is_available(project)`, which is a **public availability predicate, not
an authorization one**. The consequence made moderation self-defeating:
`admin_review_content_report` and `admin_suspend_project` both set
`is_active=False`, which instantly 404'd the reported project's own marker image
and video **for the admin too**. The only way to review the evidence behind a
report was to re-publish the reported content first. A project whose owner's
subscription merely lapsed was equally invisible.

Root-cause fix at the shared gate, not per caller: one helper,
`_admin_media_investigation_allowed()`, applied at all four sites. It is
read-only, grants no new capability, and is gated on the same
`admin.projects.view` that already guards the page the evidence renders on. It
is called **lazily** — only after a project is already judged not-publicly-live —
so the public scanner path pays no extra query. Media served under this branch
gets `Cache-Control: private, no-store`, so a suspended project's bytes never
land in a shared proxy or the admin's own disk cache.

Verified: admin loads image and video for a live project; admin still loads both
**after suspending it**, with `private, no-store`; anonymous requests still 404;
an ordinary logged-in user still 404s; a live public project keeps its ordinary
`public, max-age=3600`.

## 28. Report queue result

Already correct. `/admin/moderation` renders and `/admin/reports` returns the
queue JSON gated on `admin.reports.view`; unauthenticated access is refused.
Verified the queue contains exactly the expected report id.

## 29. Report detail result

`/admin/reports/<id>` was already gated and returning the report, but the
payload was **missing the two facts the decision depends on**. Added to
`_content_report_payload` (all derived from the reported project — no new stored
data, no migration): `project_owner_type`, `project_owner_user_id`,
`project_is_active`, `project_is_publicly_live`. Report id, project id, project
name, reason, details, created/reviewed timestamps, status, resolution action and
reason were already present.

Deliberately **not** added: the owner's email or name. Moderation needs to know
which account to hold responsible and be able to open it; it does not need the
account holder's contact details rendered into a queue view. The owner link goes
to `admin_view_user`, permission-gated in its own right.

## 30. Reporter / anonymous behaviour

Correct as found; pinned. An authenticated reporter yields
`reporter_user_id` + `has_reporter_contact: true`. An anonymous report yields
`reporter_user_id: null` + `has_reporter_contact: false` and the UI prints
"Anonymous" — **no reporter identity is ever invented**. The payload never
exposes `reporter_email`, `reporter_ip_hash` or `reporter_session_hash`; the
authenticated case is asserted to carry the reporter's id and boolean but not the
raw address. Wave 5's privacy model is unchanged.

## 31. Report reason / detail behaviour

Correct as found; pinned. Canonical reason enum round-trips (`COPYRIGHT_OR_IP`,
`PRIVACY`, `SPAM`), reporter-supplied `details` is stored and returned verbatim,
`created_at` and `status` present. Existing validation (invalid reason 400,
`DETAILS_TOO_LONG` 400, 404 on missing project, submission rate limit) re-run and
still passing. One UI fix: the modal sliced the timestamp to a date, losing
within-day ordering that an investigation needs — now shown to the second.

## 32. Report → project evidence flow

Complete for the first time. The modal already linked to
`/admin/projects/<id>`; that page now actually shows the marker image and the
video (section 27), and the media routes now authorize the admin even when the
report has already caused a suspension. The modal additionally shows the owner
with a link to their account and the project's current state, so a moderator can
see whether a suspension has already been applied before offering to apply one.

## 33. Moderation actions

Verified, unchanged, non-destructive. `admin_review_content_report` accepts
`UNDER_REVIEW` / `ACTION_TAKEN` / `DISMISSED` and explicitly rejects a return to
`OPEN`; `resolution_action` is constrained to
`{NONE, PROJECT_SUSPENDED, CREATOR_CONTACT_REQUIRED, LEGAL_REVIEW_REQUIRED, OTHER}`.
`ACTION_TAKEN` + `PROJECT_SUSPENDED` sets `is_active=False` and nothing else —
row, media and QR untouched. `admin_suspend_project` / `admin_restore_project`
are a reversible pair, each idempotent (already-suspended and already-active both
short-circuit).

**No hard delete added**, and none exists as a moderation action. Verified that a
submitted report by itself leaves `is_active` true, the project and pair present,
and `_project_is_available` true; that `PROJECT_DELETED` is rejected 400
`INVALID_ACTION` with the project intact; and that the governed action set
contains no `DELETE`/`BAN`/`BLOCK`/`REFUND`/`PURGE`/`REMOVE` verb.

## 34. Permission enforcement

Unchanged and verified. `admin.reports.view` (queue/detail), `admin.reports.manage`
(review), `admin.projects.suspend` (suspend/restore), `admin.projects.view`
(project detail, and now the media investigation branch — deliberately the
*same* permission, so no new grant). Verified unauthenticated mutation is
refused with the report left `OPEN`, and that an admin whose role has
`admin.reports.manage` and `admin.projects.suspend` removed is refused on both
review and suspend with the report status and `project.is_active` unchanged.
Existing super-admin authorization and admin-parity suites re-run clean.

## 35. Audit / activity result

Verified against the existing `AdminActivity` infrastructure; no new audit
plumbing and no new noise. `content_report_review` records report id, project id,
target status, action and a truncated reason. `project_suspend` and
`project_restore` each record their own row. Verified: dismissing writes exactly
one `content_report_review` row naming the report; review-with-suspension writes
exactly one; suspend-then-restore writes both types. Harmless page views —
opening a user, a project or the moderation queue — are deliberately **not**
logged, matching existing product intent.

## 36. Focused test results

`pytest -p no:randomly`, authoritative Python
(`F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe`). Full suite not run,
per instruction.

**588 tests, 0 failures, across the 20 affected suites.** `--collect-only` over
all 20 files reports exactly 588 collected, and the batch results below sum to
596 minus the 8 tests of `test_admin_panel_repair.py`, which appears in two
batches — 588. Every batch finished with zero failures.

| Suites | Result |
|---|---|
| `test_v11_production_ops_admin_investigation.py` (new) | **62 passed** |
| `test_resumable_upload`, `test_multi_pair_resumable_upload`, `test_rq_processing_foundation` | **101 passed** |
| `test_security_health_performance`, `test_v11_p1_backend_security_ops`, `test_wave1_p0_blockers`, `test_runtime_hardening_p0`, `test_v11_final_security_deployment` | **190 passed** |
| `test_admin_navigation_routing`, `test_admin_projects_module`, `test_domain_commercial_capacity_and_reporting`, `test_v1_agent2_admin_parity`, `test_super_admin_authorization` | **138 passed** |
| `test_admin_crud_hardening`, `test_admin_panel_repair`, `test_wave5_admin_commercial_completion`, `test_wave4_vendor_ownership_backend` | **79 passed** (1 assertion corrected first — see below) |
| `test_admin_panel_repair`, `test_project_qr_scanner_baseline`, `test_user_projects_page` | **26 passed** |

New-suite coverage by required scenario group: rate-limit 11/11,
health-readiness 10/10, upload cleanup 12/12, log redaction 4, admin/moderation
25/25 (several already covered by existing suites and re-verified rather than
duplicated).

**One existing assertion was changed, and it matters.**
`test_admin_panel_repair.py::test_suspended_project_blocks_and_restore_reenables_scanner_and_media`
asserted that after an admin suspends a project, `/image/...` and `/video/...`
return 404 **while still in that admin's session** — i.e. it encoded the
moderation defect in section 27 as expected behaviour. Rewritten to assert the
property it actually means to protect: the **public** (anonymous, and an ordinary
logged-in user) still gets 404, while the admin who ordered the suspension gets
200 with `private, no-store`. The scanner page and `/detect_init` assertions are
untouched and still 404 — the public suspension guarantee is intact.

## 37. Scanner hashes before/after

LF-normalized SHA256 (`tr -d '\r' | sha256sum`), per the established pattern
that avoids a CRLF-checkout false positive:

| File | Before | After |
|---|---|---|
| `scanner_runtime.py` | `eda140bf24f534e160d365c863c618469d68bbcf9619273d499674590324cec0` | `eda140bf24f534e160d365c863c618469d68bbcf9619273d499674590324cec0` |
| `static/js/scanner-runtime.js` | `05badbd03e00c22715edbdba168db8721ae621493acab8a211a54dbf76acc5b2` | `05badbd03e00c22715edbdba168db8721ae621493acab8a211a54dbf76acc5b2` |

**Identical.** No ORB/RANSAC/homography/optical-flow/tracking/threshold/
calibration/perspective/reacquisition/overlay code touched. No scanner template
touched — admin investigation reuses the existing media routes, so none was
needed.

## 38. Migration status

**No migration created, and none required.** Every change reuses existing
columns: `UploadSession.status` already permits `finalizing`/`assembled`/`active`/
`failed` (all in `UPLOAD_SESSION_STATUSES`); `failure_code` is an unconstrained
`String(50)`, so `FINALIZE_INTERRUPTED` needs no schema change; the report
payload additions are all derived at serialization time from the existing
`Project` row.

One defect **found but deliberately not fixed**, because it *would* require a
migration:

> `ContentReport.project_id` is `nullable=False` with a cascade from `Project`,
> so the superadmin hard-delete path (`admin_delete_project`) destroys the
> moderation reports filed against a project along with it. That is an
> audit-integrity gap — the record of *why* content was removed disappears with
> the content.
>
> - Missing structure: `ContentReport.project_id` would need to become nullable
>   with `ondelete="SET NULL"`, so reports survive as detached history (exactly
>   the pattern `UploadSession.project_id` already uses per P0-5).
> - Why existing structures cannot do it: with a `NOT NULL` FK there is no
>   representation for "report whose project is gone"; the row must either
>   cascade away or block the delete.
> - Impact: one `ALTER COLUMN … DROP NOT NULL` plus an FK redefinition on
>   `content_reports`; small table, but it rewrites a constraint.
> - Rollback: re-adding `NOT NULL` requires deleting or reassigning any detached
>   rows created in the interim, so rollback is not purely structural.
>
> Per instruction I stopped rather than create it. Hard delete is superadmin-only
> and is not a moderation action, so this is not a release blocker.

## 39. `git diff --check`

Clean — no output, no whitespace errors, no conflict markers.

## 40. `git status --short`

Clean after commit (working tree empty). Pre-commit state was 9 modified files
plus 2 new files (test suite, this report), all listed in section 5.

## 41. Remaining release blockers

**None from this lane.** Deferred, all non-blocking and documented above:

1. `ContentReport` cascade-on-hard-delete — migration required, section 38.
2. `pool_pre_ping`'s unbounded `SELECT 1` — marked in-code with the upgrade path,
   section 11.
3. `rq_worker.py`'s intentionally unbounded blocking connection, section 13.
4. Reverse-proxy and backup values are documented requirements, not verifiable
   in-repo; they need confirming against the real Hostinger/Nginx config and a
   real restore drill before go-live.
5. Alert on `finalizing` sessions older than the threshold is documented as an
   operational task; `cleanup-upload-sessions` must be scheduled for the
   recovery to actually run.

## 42. Recommendation

**Merge.** Both halves are done with evidence rather than assumption. Part A was
largely already correct — verified and pinned, with two real defects closed
(unbounded Redis sockets, unrecoverable `finalizing` sessions) and two small ones
(unbounded stale-job batch, raw exception in a reset-path log). Part B was
genuinely broken and is now repaired with the smallest changes that fix the root
causes: one uncommented template card, evidence rendered against existing routes,
and one shared authorization helper that stops a public availability check from
doubling as a staff blindfold.

588 focused tests pass across 20 suites. Scanner files byte-identical. No
migration. `git diff --check` clean. The one existing assertion I changed is
called out explicitly in section 36 — it encoded a defect as expected behaviour,
and a reviewer should confirm they agree with that reading before merging.
