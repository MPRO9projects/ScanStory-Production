# V1.1 P1 — Backend Security & Operations Hardening Report

Lane: `agent/v1.1-platform-admin` (worktree `F:\ScanStory-main\ScanStory-v1.1-agent1`)
Scope: the ten P1 backend/security/operations items plus a reassessment of the
audit's HIGH findings. No scanner work. No frontend redesign.

---

## 1. Starting HEAD

- Branch tip on entry: `42e5b2010beb860bc17132c5c2a5a698ddfd535c` (P0 tip), clean.
- Authoritative integration HEAD `5808d5bdfeed8872743e3d3e9334a95a9a0dc09a`
  (`develop/scanstory-v1.1`) was synced in first. The merge was a **fast-forward**
  (`Updating 42e5b20..5808d5b`), so there were **no conflicts** and nothing was
  resolved. Post-sync starting point for all work in this lane: `5808d5b`.
- Re-verified before merging: `git status --short` empty, `git branch --show-current`
  = `agent/v1.1-platform-admin`, `git rev-parse HEAD` = `42e5b20`.
- Local-only untracked artifacts were noted and left untouched (see §19).

## 2. Ending HEAD

`e9eeeab` after the code and test commits; final commit adds docs + this report
(see §3 for the full list — the docs commit is the last one).

## 3. Commits made

| Commit | Subject |
|---|---|
| `a486f63` | `fix(v1.1): harden P1 backend security and operations` |
| `e9eeeab` | `test(v1.1): cover P1 backend security and operations` |
| (final) | `docs(v1.1): require worker monitoring and report P1 backend hardening` |

**Why three commits and not the suggested six.** All ten items' production code
lives in exactly two files (`app.py`, `processing_queue.py`). Splitting them into
six commits would have required interactive partial staging of one 16k-line file
and would have produced six commits that each touch the same file with no
independent reviewability gain. The suggested split was adapted rather than
forced; the commit body of `a486f63` documents each of the ten items separately.

## 4. Files changed

| File | Change |
|---|---|
| `app.py` | P1-1 header helper + `send_email_smtp` + contact route; P1-2 reCAPTCHA fail-closed; P1-3 readiness worker check wiring; P1-4 expiry constants/helpers/CLI; P1-5 claim governance gate; P1-6 shared refund predicate + out-of-band read contract; P1-8 `reconcile-storage --json`/exit code; P1-9 coverage-state resolver + project-list context; P1-10 claim-lookup endpoint + rate-limit bucket |
| `processing_queue.py` | `queue_worker_state()`, `_rq_workers_for_queue()`, `_worker_stale_after_seconds()` |
| `tests/conftest.py` | stash the real `send_email_smtp` / `verify_recaptcha_v3` before installing the stubs |
| `tests/integration/test_v11_p1_backend_security_ops.py` | new — 60 focused tests |
| `tests/integration/test_rq_processing_foundation.py` | one `/ready` expectation updated for the intended P1-3 contract change |
| `tests/integration/test_wave1_p0_blockers.py` | one `/ready` expectation updated for the same reason |
| `docs/production/monitoring-alerting.md` | `/ready` contract, worker requirement, worker-count alert |
| `docs/production/deployment-runbook.md` | worker-start step, required long-running processes, scheduled maintenance commands, new stop condition |
| `V1_1_P1_BACKEND_SECURITY_OPS_REPORT.md` | this report |

**Not touched:** `models.py`, `core/config.py`, `requirements.txt`, `migrations/`,
`scanner_runtime.py`, `static/js/scanner-runtime.js`, any P1A frontend template.

## 5. Migration revision / parent / head

**None needed — verified.** Both columns this wave operationalizes already exist:
`ProjectOwnershipTransfer.expires_at` (`models.py:1151`) and
`ProjectOwnershipClaim.response_deadline_at` (`models.py:1187`). No other item
required schema change. Alembic head is unchanged at **`c1a7f3d95e24`**, and no
existing migration was edited.

## 6. SMTP / header injection — root cause and fix (P1-1)

**Root cause.** `send_email_smtp()` assigned caller-supplied strings straight into
`msg["From"]`, `msg["To"]` and `msg["Subject"]` under the legacy `compat32`
policy, which does not reject embedded CR/LF in a header value. `msg.as_string()`
then serialized the injected lines verbatim into `server.sendmail(...)`. The
reachable path was `/send-contact-email`, which built
`subject = f"[{enquiry_label}] Contact Form — {name}"` from `request.form` — and
both halves are user-controlled (`enquiry_label` falls back to the raw
`enquiry_type` for any unrecognized value, so `.title()` is the only processing
it gets). A `name` of `Attacker\r\nBcc: victim@example.com` turned the form into a
relay through the platform's own authenticated SMTP account.

**Fix — one reusable helper, at the single point every mail path already routes
through.** All five senders (`send_email_verification_otp`,
`send_reset_password_otp`, `send_payment_success_email`,
`send_admin_password_reset_email`, `_notify_ownership`) and both inline callers go
through `send_email_smtp`, so the guard lives there rather than in seven callers:

- `safe_email_header(value, field)` rejects `\r`, `\n` and `\x00` with a
  `ValueError`. **Rejects, not strips** — a newline in a name is an injection
  attempt, not a typo, and silently mangling it hides the attempt.
- `to_email`, `mail_from` and `subject` are validated **before** the message is
  built, so nothing partial can reach the wire.
- Unicode is preserved: a non-ASCII subject is RFC 2047 encoded via
  `email.header.Header(subject, "utf-8")` (it previously would have raised
  `UnicodeEncodeError` inside `smtplib`). ASCII subjects are unchanged.
- `html_body` is deliberately **not** checked — newlines in a body are ordinary
  content, and this is a header-only concern.
- The contact route pre-checks the composed subject so an injection attempt is a
  **400 with a safe message**, not a 500.
- Secondary, same finding: the contact route's `except` returned `str(e)` to an
  unauthenticated caller (SMTP host/gateway banner disclosure). It now logs with
  `app.logger.exception` and returns a generic message.
- Also escaped: the five form fields interpolated into the outbound HTML body
  (`markupsafe.escape`), so a submitter cannot author markup inside a staff inbox.

No existing legitimate email changed shape: the four templated senders pass
ASCII constants as subjects and validated addresses.

## 7. reCAPTCHA production policy (P1-2)

**Verified current behaviour before changing anything.** `verify_recaptcha_v3()`
returned `True, "OK"` unconditionally when either key was blank, with only a
warning log. The provider-failure branch **already** failed closed
(`except Exception: return False, ...`) — that was correct and is unchanged.
`RECAPTCHA_SITE_KEY`/`RECAPTCHA_SECRET_KEY` default to `""` and are absent from
`_validate_required_runtime_config()`.

**Fix.** Missing config is now a deployment fault, not a pass:

- If `_runtime_production_mode_flag_active()` (the existing production predicate
  used by the rest of the config validation) → **fail closed**, returning
  `False, "Security verification is unavailable. Please try again later."` and
  logging at ERROR level with the **names** of the missing settings only.
- Otherwise → the documented dev/test bypass is retained, unchanged. This is what
  keeps the suite usable without real keys.

**Deliberate choice: no startup requirement was added.** The audit's fix offered
"add both keys to the production-required list **or** fail closed in production
mode". The second was taken because it closes the actual exposure at the
protected submission, while adding two new hard boot requirements would change
the production boot contract that existing tests and runbooks depend on for no
additional security. Documented here as a decision, not an omission.

No key value is ever returned to a caller or written to a log.

## 8. `/ready` worker-aware behaviour (P1-3)

**Audit claim verified as true.** `redis_ready_check()` did nothing more than
`Redis.from_url(...).ping()`; `_readiness_checks()` reported `queue: "ok"` on a
reachable Redis with zero workers attached — the exact state in which every
upload queues forever and `/ready` still returns 200.

**New:** `processing_queue.queue_worker_state()` → `(state, usable_count)`:

| Condition | `checks.workers` | `/ready` |
|---|---|---|
| Redis unavailable (`redis_ready_check()` false) | not reached | 503, `queue: "unavailable"` |
| queue mode `fake`/`inline` | `not_applicable` | unchanged (200 outside production; production already refuses to boot in a non-`rq` mode) |
| `rq` mode, no `REDIS_URL` | `unavailable` | 503 |
| `rq` mode, registry unreadable | `unavailable` | 503 |
| `rq` mode, zero live workers | `unavailable` | 503 |
| `rq` mode, ≥1 live worker | `ok` | 200 |

- **Staleness:** RQ's own registry drops a worker whose heartbeat key expired; on
  top of that, a worker with a `death_date`, or a `last_heartbeat` older than
  `RQ_WORKER_STALE_AFTER_SECONDS` (default 420s, matching RQ's default
  `worker_ttl`), is not counted. Two independent guards, so an unreaped registry
  entry cannot report a dead worker as usable.
- **No leakage:** the response carries `usable_worker_count` (an integer) and
  nothing else. No worker names (which are hostname-pid strings), no current job,
  no connection string.
- **`/healthz` untouched** — it remains a bare `{"status": "ok"}` liveness probe
  and performs no queue diagnostics. A test asserts that
  `queue_worker_state` is never called from it.
- `ready()`'s decision changed from `checks.get("queue") == "unavailable"` to
  `"unavailable" in checks.values()`, so a component added later cannot be
  silently ignored the way the worker check would have been.

**Docs:** `deployment-runbook.md` gained an explicit worker-start step (17a), a
"Required Long-Running Processes" table, and a new stop condition; step 19 now
requires `checks.workers == "ok"`. `monitoring-alerting.md` documents the full
`/ready` contract, the worker requirement, and an immediate alert on
`usable_worker_count == 0`.

**Two existing `/ready` assertions were updated**, not worked around: a valid
`rq` configuration now means "a worker is attached", which is the intended
contract change. Both now monkeypatch `queue_worker_state`.

## 9. Transfer expiry implementation (P1-4)

**Verified:** the column and the check both already existed; only the assignment
was missing, so `EXPIRED` was unreachable in production. No migration needed.

- `initiate_project_ownership_transfer()` now sets
  `expires_at = now + ownership_transfer_expiry_days()` when a caller does not
  supply one. Duration is a **named, env-overridable function**
  (`OWNERSHIP_TRANSFER_EXPIRY_DAYS`, default 14) beside its claim counterpart, not
  a magic number inline. Both `PENDING_ACCEPTANCE` and `PENDING_CAPACITY` are
  covered, because `PENDING_CAPACITY` is reached on the same row.
- `expire_transfer_if_due(transfer, ...)` is the shared pre-mutation check;
  `accept_project_ownership_transfer()` (which is also the retry path for a
  `PENDING_CAPACITY` transfer) now calls it instead of an inline comparison.
- **Idempotent by construction:** the transition is the existing conditional
  UPDATE gated on a still-pending status, so a second call neither
  re-transitions nor appends a second audit entry, and an already-`EXPIRED`
  transfer still answers `True`. A `COMPLETED`/`CANCELLED`/`DISPUTED` transfer is
  never reopened or overridden by a deadline.
- **Ownership is never touched by expiry**, and a **linked claim is deliberately
  left alone** — an expired handover offer and an open claim are separate
  lifecycles, and cancelling someone's claim because a counterparty missed a
  deadline is not a decision this function may take. A test asserts the claim
  status is unchanged after the CLI expires its linked transfer.
- New CLI `flask expire-ownership-transfers [--apply]`, mirroring the existing
  `reconcile-*` / `expire-stale-reservations` pattern: dry-run default, prints
  each candidate, safe to re-run. Now listed in the runbook's scheduled commands.
- Audit history is preserved (`_record_ownership_event(..., "transfer_expired")`).

## 10. Vendor-before-admin claim workflow behaviour (P1-5)

**Current state machine, read from source first.** Statuses: `OPEN` →
(`VENDOR_NOTIFIED`) → `APPROVED_BY_VENDOR` | `PENDING_ADMIN_REVIEW` →
`APPROVED_BY_ADMIN` | `REJECTED` | `CANCELLED` | `EXPIRED` |
`TRANSFER_COMPLETED`. `respond_to_project_ownership_claim()` already lands a
vendor refusal in `PENDING_ADMIN_REVIEW` and a vendor acceptance in
`APPROVED_BY_VENDOR`. The audit's claim was confirmed: both admin adjudication
functions accepted **any** `PROJECT_ACTIVE_CLAIM_STATUSES` value, including
`OPEN`, so admin could adjudicate before any vendor had been given a chance.

**Fix — `claim_admin_review_block_reason(claim)`, consulted by both
`approve_project_ownership_claim_by_admin()` and
`reject_project_ownership_claim_by_admin()`:**

| Project | Claim status | Admin may adjudicate? |
|---|---|---|
| has `manager_vendor_user_id` | `OPEN` / `VENDOR_NOTIFIED`, deadline not passed | **No** — `PermissionError` with a safe explanation |
| has `manager_vendor_user_id` | `PENDING_ADMIN_REVIEW` or `APPROVED_BY_VENDOR` | Yes (vendor has responded) |
| has `manager_vendor_user_id` | any active status, `response_deadline_at` passed | Yes (deterministic escalation) |
| **no** `manager_vendor_user_id` | any active status incl. `OPEN` | **Yes — this is the correct governed direct-admin path**, not a bug |

- The escalation instant is real now: `create_project_ownership_claim()` populates
  `response_deadline_at = now + ownership_claim_response_days()` (default 7,
  env-overridable). Previously the column existed and nothing wrote it, so "the
  vendor never answered" had no deterministic resolution and a silent vendor could
  park a claim indefinitely.
- **No new status was invented.** The gate is expressed purely in terms of the
  states the machine already had.
- The gate is keyed on `manager_vendor_user_id`, not on "someone can respond".
  Every project has a current owner, so gating on the latter would have made
  direct admin review impossible — the vendor-managed case is the only one where a
  vendor step can actually be satisfied.
- The two admin routes already caught `PermissionError` and flash it, so no route
  change was needed.
- **No ownership moves anywhere in this path.** Admin approval opens a governed
  transfer that the claimant must still accept and that still passes both capacity
  checks; tests assert the current owner is unchanged after both approval and
  rejection.

## 11. Refund attention / recovery operational behaviour (P1-6)

Read P0's implementation first. **Most of this item was already resolved by P0**
and is recorded as such rather than rewritten:

| Sub-gap | State |
|---|---|
| Recovery logic (`recover_payment_refund` / `_recover_payment_refund`) | **Already resolved by P0** — untouched |
| `flask reconcile-refunds` operator command | **Already resolved by P0** — untouched |
| Admin retry action wired to the existing recovery helper | **Already resolved by P0** — `POST /admin/api/refunds/<id>/recover` exists, `admin.payments.refund` permission, `apply` opt-in. A test asserts the route calls the existing helper and creates no new recovery logic |
| Manual-review reason/state in the read contract | **Already resolved by P0** — `status`, `reconciliation_status`, `reconciliation_message_safe`, `failure_code`, `failure_message_safe` are all in `_payment_refund_payload` |
| Out-of-band provider-refund correlation | **Already resolved by P0** — see §12 |

**Two real gaps closed here:**

1. **Two competing definitions of "needs attention".** `/admin/api/refunds?needs_attention=1`
   used a hand-written status list while `flask reconcile-refunds` used
   `stuck_refund_query()`. They could disagree (e.g. an unconfirmed-status refund
   whose reconciliation had already been marked `APPLIED` appeared to the CLI and
   not to the API). Extracted `stuck_refund_filter()` as the single predicate and
   pointed both at it. A test asserts the endpoint's id set equals
   `stuck_refund_query()`'s, and that a settled refund (`REFUNDED` + `APPLIED`) is
   excluded.
2. **Out-of-band refunds were invisible to the API.** They have no `PaymentRefund`
   row by design, so an operator working from `/admin/api/refunds` could not see
   them at all — only the CLI listed them. `needs_attention=1` now also returns
   `out_of_band_refunds[]` + `out_of_band_total`, carrying **ids and the
   correlated local source only** (`webhook_event_id`, `event_type`,
   `payment_order_id`, `addon_purchase_id`, a `state` of
   `MANUAL_REVIEW_REQUIRED`, and a fixed reason string). No provider payload, no
   signature, no secret.

Nothing about refund scope changed: still admin-only, still full-refund-only,
still provider-confirmed before entitlement reversal, still no physical media
deletion, and manual review is still never auto-resolved (a test drives the retry
action against a `MANUAL_REVIEW_REQUIRED` refund and asserts it comes back 409
with `outcome == "manual_review"` and the row unchanged).

## 12. Out-of-band refund result (P1-7)

**Classification: already resolved by P0 — verified here, code unchanged.**

`_process_refund_webhook_event()` was read against the invariant and two focused
tests were written (there were none exercising this path):

- A provider refund that matches no local `PaymentRefund` but whose
  `payment_id` correlates to **exactly one** local commercial source is recorded
  on the webhook event with `failure_code = out_of_band_refund_no_local_record`
  plus the resolved `payment_order_id`/`addon_purchase_id`, and surfaces through
  `unlinked_out_of_band_refund_events()`. **No local refund record is fabricated**
  — a test asserts no `PaymentRefund` appears for the provider refund id, which
  is correct: `requested_by_admin_id` is the record of who authorized a refund and
  inventing one would corrupt the audit trail.
- An uncorrelatable refund gets `failure_code = unknown_refund` with **no**
  `payment_order_id`/`addon_purchase_id` attribution. `_commercial_source_for_provider_payment()`
  returns `(None, None)` unless exactly one local source owns the payment id, so
  an ambiguous case never guesses which purchase was refunded.

The only change made for this item is the read-API exposure described in §11 (an
operator now has a non-CLI way to see the queue). No behaviour change.

## 13. Storage reconciliation operational changes (P1-8)

Accounting logic in `reconcile_storage_ledger()` was **not** redesigned or
touched. Only the command surface changed:

- **`--json`** emits the whole report (mode, `discovered`, `created`,
  `already_reconciled`, `total_bytes_accounted`, per-category `counts`,
  `needs_human_total`, and the full `findings` lists — untruncated). Validated as
  parseable JSON by test, and asserted free of credentials/DB URL.
- **Non-zero exit** (`SystemExit(1)`) when a category that genuinely needs a human
  is non-empty: `ambiguous_ownership` (ownership that could not be resolved and
  was therefore left alone) or `errors` (hard reconciliation failures). Previously
  it always exited 0, so a scheduled run could look clean while storage
  accounting was unresolved.
- **Orphan files are explicitly non-blocking and never deleted.** An orphan is a
  report; a test writes an orphan file, runs `--apply`, and asserts the file and
  its bytes are still on disk and the exit code is 0.
- **Truncation now states the total:** `... and N more (total M; use --json for all)`.
  Per-category counts were already printed; the missing piece was the total on the
  truncated line and a way to get every entry.
- **Category labels clarified** in the human output: "Missing files (ledger row
  exists, file absent on disk)", "Orphan files (file on disk, no ledger row -
  reported only, never deleted)", "Ambiguous ownership (left unassigned for a
  human)", "Hard reconciliation errors".
- **One audit log line per run** (`app.logger.info reconcile_storage_run ...`) —
  a log line, not a new reconciliation-history subsystem.
- **Dry-run remains the default** (asserted by a test on the option's default) and
  `--apply` semantics are unchanged and still idempotent (asserted: a second
  `--apply` creates 0 and reports them as already reconciled).

## 14. Project-list coverage summary contract (P1-9)

**Gap verified real:** the `/projects` route set `pairs_count`,
`viewer_relationship`, `is_suspended` and `active_transfer_status` on each card
and supplied no coverage data at all, so a truthful coverage badge was
impossible.

**The authoritative resolver is reused, not duplicated.** Each card now carries
`project.coverage_summary = project_coverage_summary(project)` — the same function
the project detail page and `/api/projects/<id>/coverage` use. No coverage rule
is written in the route, and none in any template.

`project_coverage_summary()` gained **one** new key so the four states the UI
needs are distinguishable (the pre-existing `is_live`/`reason` pair collapsed
"expired" and "never covered" into a single `no_valid_coverage`):

### Contract for Agent 2

`project.coverage_summary` — a dict, present on every project in the
`user/projects.html` `projects` list. Also returned verbatim by
`project_coverage_summary()` everywhere else it is already used.

| Field | Type | Meaning |
|---|---|---|
| `project_id` | int | the project |
| **`coverage_state`** | `"active"` \| `"expired"` \| `"none"` \| `"suspended"` | **the badge field.** `"suspended"` = admin turned the project off and is deliberately NOT `"expired"`; `"expired"` = this project has been covered and that coverage ran out; `"none"` = never covered |
| `is_live` | bool | publicly reachable right now (active **and** coverage valid) |
| `reason` | `"covered"` \| `"inactive"` \| `"no_valid_coverage"` \| `"not_found"` | existing raw reason |
| `coverage_source` | str \| None | e.g. `OWNER_SUBSCRIPTION`, `ADMIN_GRANT`, `ADMIN_OWNED` |
| `effective_coverage_until` | ISO-8601 str \| None | **this is the coverage-end field.** `None` means indefinite coverage (or no coverage) — check `coverage_state` to tell those apart. Deliberately not aliased to a second name |
| `renewal_starts_at` | ISO-8601 str \| None | existing renewal anchor; `None` when indefinite coverage is active |
| `renewal_eligible` | bool | existing |
| `renewal_blocked_code` | str \| None | existing |
| `is_suspended` | bool | `not project.is_active` |

**No days-remaining field was added** — no existing logic computes it safely, and
inventing new date math was out of scope. `effective_coverage_until` is sufficient
for the UI to compute a display value.

**Query pattern / N+1.** One resolver call per card (a cached owner lookup plus
two or three indexed coverage reads). A bulk pre-computation would have required
duplicating the coverage rules, which is explicitly forbidden, so this was left
linear-per-card with an in-code `ponytail:` note naming the upgrade path (batch
inside `project_public_access_state`, not in the route). Independently, the audit
already tracks this as `STOR-5` (MEDIUM) for `project_public_access_state`
generally; this lane did not make it worse per project.

**No template was modified.** The data is available for the template to consume;
wiring and visuals are Agent 2's.

## 15. Claimant discovery / submission contract (P1-10)

**Implemented.** The safe primitive already existed, so the honest-stop clause did
not apply — but the reasoning matters and is recorded here.

**Identifier chosen: the project id already printed into every QR code.**
`Project` has no `public_key`/share-token column; `scanner_url` is
`/scanner/<int:project_id>`, and `/scanner/<project_id>` is a **public,
unauthenticated** route that already discloses, to anyone at all, that a project
exists and what its **name** and **creator display name** are. The claim
submission route `POST /projects/<int:project_id>/ownership-claim` also already
accepted any project id from any logged-in user. So keying discovery on the
project id introduces **no new disclosure primitive**; a tokenized
`/s/<share_token>` reference would be strictly better and is already flagged as a
required next phase in the `scanner()` docstring, but it does not exist yet and
inventing it was out of this lane's scope.

### Contract for Agent 2

`GET /api/ownership/claim-lookup/<int:project_id>` — `@login_required`,
read-only, rate-limited (`ownership_claim_lookup`, 30/hour per IP+user).

Eligible response:

```json
{
  "success": true,
  "eligible": true,
  "reason_code": "CLAIMABLE",
  "reason": "You can file an ownership review request. Nothing changes until it is reviewed.",
  "project": {"id": 12, "name": "Wedding Album"},
  "existing_claim_id": null,
  "claim_url": "/projects/12/ownership-claim"
}
```

- `reason_code` is one of `CLAIMABLE`, `ALREADY_OPEN`, `NOT_CLAIMABLE`.
- `ALREADY_OPEN` returns `eligible: false` with `existing_claim_id` set — the
  existing active-claim dedupe in `create_project_ownership_claim()` is unchanged
  and still authoritative.
- Every non-entitled case — project does not exist, admin-owned platform project,
  not publicly available (suspended / no valid coverage), caller already
  owns/manages it — returns **one byte-identical `NOT_CLAIMABLE` body** with
  `project: null` and `claim_url: null`. A test asserts the response for a random
  nonexistent id is `==` the response for a real-but-suspended project, and that
  the real project's name does not appear.
- The only fields ever echoed are `id` and `name`, and only while the public
  scanner page would itself serve them. No owner identity, no counts, no media.
- 429 (with `Retry-After`) when the bucket is exhausted.
- Ownership cannot change here; a test asserts filing a claim through this path
  leaves the current owner unchanged.

**Residual constraint documented:** existence disclosure through this endpoint is
bounded by the pre-existing public `/scanner/<id>` surface, not eliminated. It is
strictly narrower (authenticated + rate-limited where `/scanner` is neither), but
a genuinely enumeration-proof discovery path needs a share-token/claim-token
reference format that this codebase does not yet have. That primitive is the
correct follow-up and was not invented here.

## 16. Focused tests and exact counts

New file `tests/integration/test_v11_p1_backend_security_ops.py` — **60 passed**.

| Area | Tests |
|---|---|
| P1-1 SMTP header injection | 10 |
| P1-2 reCAPTCHA policy | 5 |
| P1-3 worker-aware readiness | 8 |
| P1-4 transfer expiry | 6 |
| P1-5 claim governance | 6 |
| P1-6 refund attention/recovery | 3 |
| P1-7 out-of-band refund | 2 |
| P1-8 storage reconciliation | 5 |
| P1-9 coverage summary contract | 5 |
| P1-10 claimant discovery | 6 |

Existing-suite regression checks (focused, scoped — the full suite was **not**
run, per lane policy):

| Suite(s) | Result |
|---|---|
| `test_rq_processing_foundation.py`, `test_wave1_p0_blockers.py`, `test_security_health_performance.py`, `test_runtime_hardening_p0.py` | **139 passed** |
| `test_wave4_vendor_ownership_backend.py`, `test_domain_ownership_foundation.py`, `test_v11_p0_project_delete_history.py` | **55 passed** |
| `test_v11_p0_refund_recovery.py`, `test_admin_refunds.py`, `test_wave5_admin_commercial_completion.py`, `test_razorpay_webhook_reconciliation.py`, `test_wave3_storage_accounting.py` | **139 passed** |
| `test_user_projects_page.py`, `test_auth_baseline.py`, `test_v1_agent2_admin_parity.py`, `test_v11_p0_config_and_gaps.py` | **91 passed** |

**Total: 484 passed, 0 failed.** No PostgreSQL certification was attempted (no
migration added; explicitly the human's job).

## 17. `git diff --check`

Clean — exit 0, no whitespace errors, on every commit in this lane.

## 18. Scanner / runtime untouched confirmation

`git hash-object` at the starting commit and at the end of the lane:

| File | Start | End |
|---|---|---|
| `scanner_runtime.py` | `5fdc3ff81e356cfad5c4896f0f9ec6bc9c6bf989` | `5fdc3ff81e356cfad5c4896f0f9ec6bc9c6bf989` |
| `static/js/scanner-runtime.js` | `b14a4ca6eb9619f81ddee1eb00075c15b1fd64a4` | `b14a4ca6eb9619f81ddee1eb00075c15b1fd64a4` |

Byte-identical. Neither file appears in `git diff --stat 5808d5b HEAD`. No
OpenCV/ORB/RANSAC/homography/optical-flow code was read or changed. The one
scanner-adjacent read was `/scanner/<project_id>`'s route body, and only to
establish what the public surface already discloses for P1-10; it was not
modified.

## 19. Integration worktree untouched confirmation

`F:\ScanStory-main\ScanStory-integration` was accessed **read-only** (`git status
--short`, `git rev-parse HEAD`, `git branch --show-current`, and reading
`SCANSTORY_V1_1_PRODUCTION_READINESS_AUDIT.md` for §20). Its HEAD is unchanged at
`5808d5b` on `develop/scanstory-v1.1`. No file was written, staged, committed or
merged there, and nothing was merged from this lane into integration.

Local-only untracked artifacts observed there and deliberately left alone:
`SCANSTORY_V1_1_PRODUCTION_READINESS_AUDIT.md`, `SCANSTORY_V1_GAP_AUDIT.md`,
`instance/`, `routes_map.txt`, and one shell-mangled filename. Note: the audit
this lane reassesses exists **only** as an untracked file in that worktree — it is
not committed anywhere on `develop/scanstory-v1.1`, which is worth fixing outside
this lane.

## 20. Remaining HIGH findings classification

Source: `SCANSTORY_V1_1_PRODUCTION_READINESS_AUDIT.md` (19 HIGH findings; the
`MU-*`, `UX-*`, `CFG-3/4/5` and `PAY-5` rows are cross-references to these, not
additional findings). Each was verified against current code, not assumed from
its original wording.

| # | Finding | Classification | Verification |
|---|---|---|---|
| SEC-1 | SMTP header injection via contact form | **RESOLVED IN THIS P1 LANE** | `safe_email_header()` rejects CR/LF/NUL in every header-bound value; 10 tests |
| SEC-2 | reCAPTCHA fails open, not production-validated | **RESOLVED IN THIS P1 LANE** | production-flagged runtime fails closed; dev/test bypass retained; 5 tests |
| SEC-3 | Razorpay credentials never validated at startup | **STILL OPEN — OPERATIONS/DEPLOYMENT** | `_validate_required_runtime_config()` still declares it "does not validate payment credentials"; `RAZORPAY_KEY_ID`/`_SECRET`/`RAZORPAY_WEBHOOK_SECRET` absent from the production-required list. Out of this lane's ten items |
| SEC-4 | CSP ships report-only by default | **STILL OPEN — OPERATIONS/DEPLOYMENT** | `CSP_ENFORCE = _env_flag("SECURITY_CSP_ENFORCE", default=False)` unchanged; flag not production-required. In-code comment says to flip it only after real-device QA |
| CFG-2 | `requests` imported but undeclared | **RESOLVED BY P0** | `requirements.txt:17` `requests<=2.34.2` |
| PAY-2 | No admin UI for the refund `needs_attention` worklist | **STILL OPEN — FRONTEND** | backend read contract is now complete (§11) incl. out-of-band rows; there is still no `admin/refunds.html`. Agent 2 |
| PAY-3 | Out-of-band dashboard refunds never ingested | **RESOLVED BY P0** | correlation verified by two new focused tests (§12); read-API exposure added this lane |
| PAY-4 | No refund reconciliation CLI | **RESOLVED BY P0** | `flask reconcile-refunds` with dry-run default and non-zero exit on unresolved |
| OWN-1 | `/ownership` has no navigation entry | **RESOLVED BY P1A** | `url_for('ownership_center')` now present in `user/dashboard.html` (×2), `user/profile.html` (×2), `user/projects.html` |
| OWN-2 | Claim submission surfaced only to managing vendors | **STILL OPEN — FRONTEND** | backend discovery contract shipped this lane (§15); the claimant-facing entry point is UI work. Agent 2 |
| OWN-3 | Transfer expiry is dead code | **RESOLVED IN THIS P1 LANE** | `expires_at` populated, pre-mutation check, CLI, 6 tests |
| OWN-4 | Admin may adjudicate before any vendor response | **RESOLVED IN THIS P1 LANE** | `claim_admin_review_block_reason()` gates both admin actions; deadline populated; 6 tests |
| COV-1 | Project list shows no coverage state | **STILL OPEN — FRONTEND** | backend contract shipped this lane (§14); no template changed. Agent 2 |
| COV-2 | No per-project coverage-expiry warning | **STILL OPEN — FRONTEND** | `effective_coverage_until` + `coverage_state` now available per card; the warning UI is Agent 2's |
| COV-3 | Admin coverage-grant endpoint has no UI control | **STILL OPEN — FRONTEND** | zero templates post to `/admin/projects/<id>/service-coverage/grant`; the endpoint works and is permission-gated. Agent 2 |
| DEAD-1 | Delete-admin form missing CSRF token | **RESOLVED BY P0** | `templates/admin/edit_admin.html:258` now carries `csrf_token` |
| RSP-1 | 12 admin tables without horizontal-overflow wrappers | **RESOLVED BY P1A** | `templates/admin/base.html` now defines `.table-responsive`/`.table-container { overflow-x: auto }` and all 12 named templates resolve an `overflow-x: auto` rule |
| OPS-1 | `/ready` reports ready with zero workers; runbook never starts the worker | **RESOLVED IN THIS P1 LANE** | worker-aware `/ready` (§8) + runbook step 17a + required-process table + monitoring alert; 8 tests |
| OPS-2 | Recovery CLIs exist but are never scheduled/documented | **RESOLVED IN THIS P1 LANE** | new "Scheduled Maintenance Commands" table in `deployment-runbook.md` covering all seven CLIs with cadence and the meaning of a non-zero exit |

**Totals:** RESOLVED IN THIS P1 LANE **6** · RESOLVED BY P0 **4** · RESOLVED BY
P1A **2** · STILL OPEN — FRONTEND **5** · STILL OPEN — OPERATIONS/DEPLOYMENT **2**
· STILL OPEN — BACKEND **0** · DEFERRED WITH REASON **0**.

Also noted while verifying (not HIGH, not touched, listed so it is not lost):
the older `SCANSTORY_V1_1_END_TO_END_PRODUCTION_AUDIT.md` HIGH items ANM-15/16/17
(`0` treated as unlimited; four "unlimited" sentinels `None`/`999999999`/`999999`/`0`;
admin grants writing `subscribed_*` directly) are still present in `models.py`
and `app.py`. They are entitlement-architecture items outside this lane's ten
items and were deliberately not changed. Likewise ANM-12's remaining `str(e)`
returns at `app.py:11497` (`/verify-payment`) and `app.py:13292`
(`/api/scanner/.../session-end`) — the contact-form half named by that finding is
fixed here; the payment and scanner-adjacent halves were left alone (the latter
deliberately, being scanner-adjacent with an existing contract test).

## 21. Items explicitly left for Agent 2 (final UI)

1. **Refund attention worklist** (`admin/refunds.html` or equivalent) — consume
   `GET /admin/api/refunds?needs_attention=1`, render both status axes side by
   side (`status` and `reconciliation_status` — never merged), render the new
   `out_of_band_refunds[]` block, and wire a confirm-gated retry to
   `POST /admin/api/refunds/<id>/recover` with `apply=true`. A 409 with
   `outcome: "manual_review"` must read as "a human must decide", not "failed".
2. **Per-project coverage badges on the project list** — consume
   `project.coverage_summary.coverage_state` (§14). `"suspended"` must not render
   as `"expired"`.
3. **Per-project coverage-expiry warning** — from
   `coverage_summary.effective_coverage_until` (`None` = indefinite; disambiguate
   with `coverage_state`).
4. **Admin coverage-grant control** — a form posting to
   `/admin/projects/<id>/service-coverage/grant` (`superadmin.capacity.manage`),
   which has no UI at all today.
5. **Claimant entry point** — call
   `GET /api/ownership/claim-lookup/<project_id>` (§15) from wherever a claimant
   arrives with a QR/scanner reference, then post to the returned `claim_url`.
   Treat `NOT_CLAIMABLE` as a single opaque outcome; do not branch on it in a way
   that re-creates an existence oracle.
6. **Transfer-deadline display** — `transfer.expires_at` is now always populated;
   the ownership screens can show "expires on …" and an `EXPIRED` state that was
   previously unreachable.
7. **Vendor-before-admin messaging** — the admin ownership screen should indicate
   when a claim is awaiting vendor response (admin actions now return a
   `PermissionError` message rather than acting); ideally disable the buttons for
   that state rather than surfacing a flash error after the fact.

## 22. PostgreSQL certification requirement

**None.** No migration was added (§5); Alembic head remains `c1a7f3d95e24` and no
existing migration was edited. Nothing in this lane changes schema, and no
PostgreSQL re-certification is triggered by it.

## 23. Known limitations

1. **P1-2 has no startup validation** — a production deploy missing reCAPTCHA keys
   boots successfully and fails the protected submissions closed, logging at ERROR
   level. Deliberate (§7): it closes the exposure without adding a new hard boot
   requirement. An operator watching logs, not startup, learns about it.
2. **P1-9 is one resolver call per project card.** Correctness was prioritized
   over query count because the alternative was duplicating coverage rules. The
   underlying per-project query pattern is the audit's existing `STOR-5` (MEDIUM).
3. **P1-10 does not eliminate project-existence disclosure**, it only narrows it to
   an authenticated, rate-limited surface no wider than the public
   `/scanner/<id>` page. A share-token reference format is the real fix and does
   not exist yet.
4. **Worker staleness is heartbeat-based, not liveness-probed.** A worker process
   that is wedged but still heartbeating counts as usable. RQ exposes no stronger
   signal; `RQ_WORKER_STALE_AFTER_SECONDS` is the tuning knob.
5. **`reconcile-storage --json` prints untruncated findings**, so a badly drifted
   installation can produce a large document. Intentional — the alternative was
   silent truncation, which was the defect.
6. **No PostgreSQL run of anything in this lane**; SQLite only, per lane policy.
   The `stuck_refund_filter()` / `expired_pending_transfer_query()` predicates are
   plain SQLAlchemy with no dialect-specific constructs.
7. **Five HIGH findings remain frontend-blocked and two operations-blocked** (§20).
   None of them is a backend gap.
8. **Full suite not run.** 484 focused tests across the areas this lane touches
   plus their neighbours; a full regression remains the human's step.

## 24. Final verdict

All ten P1 items are complete: six were implemented here, and the parts already
delivered by P0 (refund recovery logic, the reconcile CLI, out-of-band
correlation) were verified against current code and classified as such rather
than rewritten. No release-relevant backend gap remains in this lane's scope: the
two remaining HIGH items outside it (SEC-3 Razorpay startup validation, SEC-4 CSP
enforcement) are operations/deployment configuration decisions, and the other
five are frontend work with their backend contracts now shipped and documented.

**P1 BACKEND COMPLETE — READY FOR FINAL UI COMPLETION**
