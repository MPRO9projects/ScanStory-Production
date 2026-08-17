# V1.1 Final UI Completion Report

Lane: `agent/v1.1-experience-ux` (worktree `F:\ScanStory-main\ScanStory-v1.1-agent2`)
Scope: the six remaining frontend surfaces for the P1 backend contracts, plus a
reassessment of the HIGH findings still classified FRONTEND after P1. No scanner
work. No redesign. No new backend business logic.

---

## 1. Starting HEAD

- Branch tip on entry: `eff937e9a857804ab1973b453dc5468d60f57ccb` (the P1A
  reconciliation tip). Re-verified before merging: `git status --short` empty,
  `git rev-parse --abbrev-ref HEAD` = `agent/v1.1-experience-ux`,
  `git rev-parse HEAD` = `eff937e9a857804ab1973b453dc5468d60f57ccb`.
- Authoritative integration HEAD `fb02d4d` (`develop/scanstory-v1.1`) was synced
  first. The merge was a **fast-forward** (`Updating eff937e..fb02d4d`), so there
  were **no conflicts** and nothing was resolved. Post-sync starting point for all
  work in this lane: **`fb02d4d`**.
- The sync brought in the full P1 backend/security/ops lane (`a486f63`,
  `e9eeeab`, `fbb2271`, `336d604` plus the merge commit).
- No local-only untracked artifacts existed in this worktree at entry beyond the
  new test file this lane adds.

## 2. Ending HEAD

The tip of `agent/v1.1-experience-ux` after the third commit below, i.e. the
`docs(v1.1): report final ui completion` commit that adds this file. A commit
cannot contain its own hash, so the exact SHA is reported to the orchestrator
rather than embedded here; the two code commits it sits on are `c336d76` and
`83f34be`.

## 3. Commits made

| Commit | Subject | Contents |
|---|---|---|
| `c336d76` | `feat(v1.1): complete refund and coverage admin ux` | F1, F2, F3 + all of the `app.py` display wiring |
| `83f34be` | `feat(v1.1): complete ownership claim and expiry ux` | F4, F5, F6 + the new test file |
| *(this commit)* | `docs(v1.1): report final ui completion` | this report |

Three commits, matching the suggested split. The whole `app.py` display-wiring
block lands in the first commit as one unit rather than being split across both:
separating four small `render_template` context additions inside a 16k-line file
would have needed interactive partial staging for no reviewability gain, and each
commit still leaves the application working (unused template context is inert).
The new test file lands with the second commit because it exercises the ownership
templates that commit introduces.

## 4. Files changed

| File | Change |
|---|---|
| `app.py` | display-only wiring: `PROJECT_COVERAGE_STATE_LABELS` + jinja global; `attention_refunds` / `out_of_band_refunds` into `admin_operations()`; `expired_transfers` into `ownership_center()`; `admin_block_reason` into `_admin_claim_row()` |
| `templates/admin/operations.html` | F1 refund attention/recovery worklist + out-of-band block + confirm-gated recovery action; corrected the now-stale "worker count is not reported" sentence |
| `templates/user/projects.html` | F2 per-card coverage badge + per-state warning copy + one CSS rule |
| `templates/admin/view_project.html` | F3 grant explainer copy; coverage state now read from the resolver field instead of recomputed in Jinja |
| `templates/user/ownership.html` | F4 claimant discovery section + script; F5 transfer deadlines and terminal EXPIRED section; F6 claimant-facing review-stage copy and owner-side response deadline |
| `templates/admin/ownership.html` | F5 transfer expiry presentation; F6 vendor-block presentation replacing premature approve/reject controls; review-order explainer |
| `tests/integration/test_v11_final_ui_completion.py` | new — `36` focused tests |
| `V1_1_FINAL_UI_COMPLETION_REPORT.md` | this report |

**Not touched:** `scanner_runtime.py`, `static/js/scanner-runtime.js`, `models.py`,
`migrations/`, `core/config.py`, `processing_queue.py`, refund recovery logic,
ownership transition logic, the coverage resolver, `requirements.txt`.

The `app.py` diff is **44 lines** and contains no branch, no comparison and no
state derivation — every added line either registers display labels or passes an
already-existing helper's result into a template.

## 5. Backend contracts consumed (verified against code, not the report)

| # | Contract | Verified at | Notes |
|---|---|---|---|
| 1 | `stuck_refund_filter()` / `stuck_refund_query()` | `app.py:10683` / `app.py:10700` | the ONE attention predicate; a settled refund (`REFUNDED` + `APPLIED`) matches neither branch, so exclusion is the predicate's, not the template's |
| 2 | `PaymentRefund` read fields | `_payment_refund_payload`, `app.py:10248` | rendered: `id`, `status`, `reconciliation_status`, `reconciliation_message_safe`, `failure_code`, `failure_message_safe`, `user_id`, `project_id`, `payment_order_id`, `addon_purchase_id`, `provider_refund_id`, `amount`, `currency`, `requested_at`. **Not** rendered: `provider_payment_id`, `provider_status` |
| 3 | `unlinked_out_of_band_refund_events()` | `app.py:10905` | `RazorpayWebhookEvent` rows with `failure_code == OUT_OF_BAND_REFUND_FAILURE_CODE`; rendered fields are `id`, `event_type`, `payment_order_id`, `addon_purchase_id` only |
| 4 | `POST /admin/api/refunds/<int:refund_id>/recover` | `app.py:15314` | `admin.payments.refund`; body `{"apply": true}`; 409 when `outcome in REFUND_RECOVERY_UNRESOLVED_OUTCOMES` (`unresolved`, `retry_failed`, `manual_review`) |
| 5 | `_recover_payment_refund()` manual-review branch | `app.py:10775` | `reconciliation_status == "MANUAL_REVIEW_REQUIRED"` returns `manual_review` and changes nothing — this is the exact condition the UI uses to withhold the retry control |
| 6 | `project.coverage_summary` | set in `projects_page()`, `app.py:7214` | from `project_coverage_summary()`, `app.py:2769` |
| 7 | `coverage_state` values | `project_coverage_state()`, `app.py:2752` | `"suspended"` \| `"active"` \| `"expired"` \| `"none"`, in that decision order |
| 8 | `effective_coverage_until` | `project_public_access_state()`, `app.py:2833` | ISO-8601 string or `None`; `None` + `coverage_state == "active"` is the legacy-indefinite case |
| 9 | `POST /admin/projects/<int:project_id>/service-coverage/grant` | `app.py:16009` | `superadmin.capacity.manage`; form keys `days` (int > 0) and `reason` (non-empty, both enforced in `admin_grant_project_service_coverage`); returns 201 JSON. **No revoke route exists** — grepped, one match for `service-coverage` in the whole file |
| 10 | `GET /api/ownership/claim-lookup/<int:project_id>` | `app.py:7521` | `@login_required`, rate-limited `ownership_claim_lookup`; eligible body carries `project{id,name}`, `existing_claim_id`, `claim_url`; every non-eligible case returns one identical body |
| 11 | `POST /projects/<int:project_id>/ownership-claim` | `submit_project_ownership_claim` | form key `evidence_summary`; redirects to `/ownership` with a flash |
| 12 | `ProjectOwnershipTransfer.expires_at` | `models.py:1151` | populated by `initiate_project_ownership_transfer()` since P1-4 |
| 13 | `ProjectOwnershipClaim.response_deadline_at` | `models.py:1187` | populated by `create_project_ownership_claim()`, `app.py:2362` |
| 14 | `claim_admin_review_block_reason(claim)` | `app.py:2444` | consulted by both admin adjudication functions; resolved once per row into `row.admin_block_reason` |
| 15 | `user_can_respond_to_claim(user, claim)` | `app.py:2373` | already gates `incoming_claims` in `ownership_center()`; unchanged |
| 16 | `PROJECT_ACTIVE_TRANSFER_STATUSES` / `PROJECT_ACTIVE_CLAIM_STATUSES` | `models.py:1032` / `models.py:1044` | `{PENDING_ACCEPTANCE, PENDING_CAPACITY, DISPUTED}` and `{OPEN, VENDOR_NOTIFIED, PENDING_ADMIN_REVIEW, APPROVED_BY_VENDOR}` |
| 17 | `PROJECT_TRANSFER_STATUS_LABELS` / `PROJECT_CLAIM_STATUS_LABELS` | `app.py:1717` / `app.py:1725` | already jinja globals, already include `EXPIRED` |
| 18 | `admin_can(...)` / `admin_has_permission` / `ADMIN_ROLE_PERMISSIONS` | `app.py:2948` / `2918` / `1599` | role `admin` has `admin.payments.view` but not `admin.payments.refund`, and not `superadmin.capacity.manage` |

## 6. Refund attention / recovery UI (F1, PAY-2)

Placed on **`templates/admin/operations.html`**, the existing admin operations /
refunds surface (it already hosts the add-on refund controls, the
`_admin_fetch_helper.html` include and the CSRF-header fetch pattern). No new nav
concept, no new template, no new route.

**Read path.** The route now passes `stuck_refund_query().limit(50).all()` — the
literal predicate `flask reconcile-refunds` and
`/admin/api/refunds?needs_attention=1` share — so the screen, the CLI and the API
cannot disagree. Server-rendered rather than JS-rendered so it works without
JavaScript and is directly assertable.

**Surfaced per row:** refund id, amount + currency + "(full)", provider refund
reference (or "not issued yet"), requested-at, user id, ScanStory id, the linked
payment order or add-on purchase, **refund status and reconciliation status as two
separate columns that are never merged**, `failure_code`, `failure_message_safe`,
`reconciliation_message_safe`, and whether an operator action exists.

**Manual review is distinguished, not disguised.** A row with
`reconciliation_status == MANUAL_REVIEW_REQUIRED` gets **no retry control at all**
and an explicit sentence: *"Manual decision required. This is not automatically
fixable: an admin has to reconcile the entitlement by hand. Retrying will not
resolve it."* That mirrors `_recover_payment_refund()`'s branch 2 exactly, so the
UI never offers an action the backend would answer 409 to.

**Settled refunds are excluded** by the predicate itself (`REFUNDED` + `APPLIED`
matches neither branch of `stuck_refund_filter()`).

**Out-of-band block.** A second table lists
`unlinked_out_of_band_refund_events()` — webhook event id, event type, the
correlated local purchase, and a fixed `Manual review required` state — with copy
saying these always need a human because there is no local refund record to
recover. No `PaymentRefund` row is fabricated anywhere.

**Action.** One `Recover / reconcile` button per retryable row, `POST` with
`X-CSRFToken`, `{"apply": true}`, behind a `window.confirm` naming the four
guarantees (provider read first, no second refund, entitlements only after
provider confirmation, nothing deleted). A 409 whose `recovery.outcome` is
`manual_review` is rendered as *"A human has to reconcile this one: …"* and does
**not** reload as if it failed. Visible only with `admin.payments.refund`;
otherwise the cell reads "Read-only. Recovery needs the refund permission."

**No leakage.** No provider payload (the codebase deliberately never persists the
raw webhook body — only a `payload_hash`), no `payload_hash`, no
`idempotency_key`, no `provider_payment_id`, no secrets, and no duplicate-refund
path (the only mutation is the existing recovery route on the existing row).

## 7. Project coverage badge / warning UI (F2, COV-1 + COV-2)

On the existing `.ss-chip` badge row in **`templates/user/projects.html`** — a
status enhancement inside the existing card language, not a redesign.

The template branches on **one** thing: the backend string
`project.coverage_summary.coverage_state`. It performs no date comparison and no
availability derivation. Four chips:

| `coverage_state` | Chip | Tone | Extra |
|---|---|---|---|
| `active` | "Coverage active" | ok | `· until <date>` when `effective_coverage_until` is set, `· no end date` when it is `None` |
| `expired` | "Coverage expired" | stop | warning paragraph |
| `none` | "No coverage" | wait | warning paragraph |
| `suspended` | "Suspended by ScanStory" | stop | warning paragraph with different advice |

**The indefinite case is represented faithfully.** `effective_coverage_until is
None` with an `active` state renders "no end date" — never a fabricated date.

**SUSPENDED never reads as EXPIRED.** Different label, different warning, and the
suspended warning explicitly says *"buying coverage will not lift a suspension"*
so nobody is sent to pay for something that would not restore their ScanStory.
The expired/no-coverage warnings say the opposite (renew and the same QR works).
All three warnings state that media and QR codes are kept.

**Near-expiry: deliberately not built.** There is no truthful backend signal for
it. `project_coverage_summary()` has no days-remaining or near-expiry field, and
Agent 1's report states one was deliberately not added. Inventing a threshold in
Jinja would be exactly the duplication this lane forbids, so the badge shows the
real end date for active coverage and nothing is faked. Recorded in §19.

Colour never carries meaning alone: every chip pairs an icon with text, and the
warning paragraphs are prose.

## 8. Admin coverage grant UI (F3, COV-3)

**Discrepancy found first (see §18): the grant form already existed.** It was
added by P1A commit `4ee2993` in this very lane and posts to the real endpoint
with the real field names. Agent 1's P1 report classified COV-3 as STILL OPEN with
"zero templates post to `/admin/projects/<id>/service-coverage/grant`", which was
already untrue at Agent 1's own starting commit. Verified by
`git log -S "coverageGrantForm" -- templates/admin/view_project.html`.

Verified intact against the real route (`superadmin.capacity.manage`, `days` int,
`reason` required, CSRF hidden field, `window.confirm` before submit, `role="status"`
`aria-live="polite"` result line, no revoke control). What this lane added is the
part that was genuinely missing:

- A visible **explainer panel** (`data-testid="coverage-grant-explainer"`) stating
  that this grants *project service coverage only*, that it is separate from the
  account's subscription plan, that it does not extend/change/create a
  subscription, does not change project slots or scan allowance, and does not
  change ownership or billing history. Previously that reassurance existed only
  inside the `confirm()` dialog.
- That a grant is a finite number of days from the project's renewal anchor and
  that the reason is recorded in the admin activity log.
- That coverage **cannot be revoked from this screen** (there is no revoke
  endpoint) and that suspension is the way to take a project offline.
- The two on-page "Coverage State" rows now render the resolver's
  `coverage_state` through `PROJECT_COVERAGE_STATE_LABELS` instead of the old
  Jinja `is_suspended`/`is_live` recomputation, which collapsed expired and
  never-covered into a single "Not live".
- `Covered Until` now formats the date and prints "No end date" only when
  coverage is actually active, instead of the ambiguous "Indefinite / none".

No revoke, delete or amend control was invented.

## 9. Claimant discovery / claim entry UI (F4, OWN-2)

Placed in **`templates/user/ownership.html`** (the Ownership Center), which is the
claimant's existing hub, is already linked from Dashboard / My Stories / Profile
after P1A, is `@login_required` like the lookup endpoint, and already lists the
claimant's own requests directly below.

**The reference is the scanner link already printed with the QR code.** The field
asks for the pasted `…/scanner/<id>` URL; a small regex lifts the id out of it (a
bare numeric reference is also accepted, since it is trivially derivable from the
same link). This is the identifier Agent 1's P1-10 chose precisely because
`/scanner/<project_id>` is already a public page. **No new enumeration surface**:
there is no listing, no browsing, no id iteration, and the only thing the control
can do is call the existing authenticated, rate-limited endpoint.

**Eligible** → a claim form is rendered posting to the server-supplied
`claim_url`, with `csrf_token`, an `evidence_summary` textarea, and a
`window.confirm` stating it does not transfer the ScanStory and that a person
reviews every request. Focus moves to the textarea.

**Not eligible** → **one code path** prints the server's own sentence. The script
branches only on `eligible`; it never reads `reason_code`. So an
already-open request, an owner/manager self-claim attempt, a suspended project, an
admin-owned project and a nonexistent id are indistinguishable on screen, exactly
as the byte-identical server response intends. An unparsable reference prints the
**same generic sentence**, so even a client-side rejection is not an oracle. No
raw enum string (`CLAIMABLE`, `ALREADY_OPEN`, `NOT_CLAIMABLE`) appears anywhere in
the page — asserted by test.

**Self-claim is not encouraged.** The server answers non-eligible for anyone who
owns or manages the project, so no CTA appears for them. 429 is handled with a
"wait a little" message.

**Copy.** The section states that filing never transfers a ScanStory, that the
request goes to the current owner or their managing vendor first and to the
ScanStory team afterwards if there is no vendor or no answer, and that ownership
moves only when a handover is opened and accepted.

**The public scanner page was deliberately not modified.** `templates/user/scanner.html`
is a 7,650-line full-screen AR camera experience with no static footer, and this
lane must not disturb scanner behaviour. Recorded as a limitation in §19.

## 10. Transfer expiry / EXPIRED UI (F5, OWN-3 presentation)

**Ownership Center (`templates/user/ownership.html`).**

- Every pending handover now shows the backend-set deadline. Recipient side:
  "Respond by <date>. After that this handover expires and the sender has to start
  a new one. Ownership does not move either way." Sender side: "The recipient has
  until <date> to respond…".
- A new **"Expired handovers"** section lists terminal `EXPIRED` transfers where
  the viewer is recipient, from-owner or initiator. It carries **no accept, retry,
  decline or withdraw control** — kept in its own list precisely so a terminal
  state cannot inherit an actionable list's buttons.
- Its copy states ownership did not move, that media and QR stayed where they
  were, and that **a linked ownership review request is separate and still listed
  below with its own status** (expiry does not cascade-cancel a claim).
- `PENDING_CAPACITY` and `DISPUTED` copy is byte-unchanged from the P1A
  reconciliation work, and both remain in the actionable lists. Regression-tested.

**Admin ownership (`templates/admin/ownership.html`).**

- The status cell carries `data-transfer-status`, a per-state badge colour, the
  raw status, the label, and for `PENDING_ACCEPTANCE`/`PENDING_CAPACITY` an
  "Expires <date>" line.
- `EXPIRED` gets its own sentence: closed on its deadline, ownership did not move,
  no action is available, and a linked review request was not cancelled by the
  expiry. The action cell already rendered "No available action" for `EXPIRED`,
  and still does.
- Claims show a "Response deadline <date>" line while `OPEN`/`VENDOR_NOTIFIED`.

**No expiry is computed in the frontend.** `expire_transfer_if_due()` and the CLI
remain the only things that transition a transfer to `EXPIRED`; the UI formats
`expires_at` and gates controls off the real `status` string. A transfer whose
deadline has passed but which the backend has not yet transitioned still renders
as pending with its deadline shown — which is the truth.

## 11. Vendor-awaiting-response claim UI (F6, OWN-4 presentation)

**Admin side.** The route resolves `claim_admin_review_block_reason(claim)` once
per row into `row.admin_block_reason` — the backend's own answer, not a Jinja
restatement of its condition. The decision cell is now a three-way branch in the
same order the backend enforces:

1. claim status **not** in `PROJECT_ACTIVE_CLAIM_STATUSES` → "No available
   decision" (mirrors the `ValueError` both admin functions raise first).
2. `admin_block_reason` set → **no approve/reject forms at all**, replaced by a
   warning panel: *"Waiting on the managing vendor."* plus the backend's own
   explanation. Approve/reject would raise `PermissionError` in this state, so the
   control is withheld instead of failing after the click.
3. otherwise → the existing approve and reject forms, unchanged.

This reproduces the real gate's behaviour, including the parts that are *not*
blocks: `PENDING_ADMIN_REVIEW` and `APPROVED_BY_VENDOR` are adjudicable, a passed
`response_deadline_at` escalates, and **a project with no managing vendor keeps the
direct-admin path on an `OPEN` claim** — all three regression-tested.

The page header explains the review order in words, including that a project
without a managing vendor goes straight to admin review.

**Claimant side.** Each of the claimant's own requests now carries a per-status
explanation for `OPEN`, `VENDOR_NOTIFIED`, `APPROVED_BY_VENDOR`,
`PENDING_ADMIN_REVIEW`, `APPROVED_BY_ADMIN`, `REJECTED`, `CANCELLED` and
`EXPIRED` (the existing `TRANSFER_COMPLETED` copy is untouched). Every
pre-completion state says "Ownership has not changed" / "has not changed yet".
Whether a vendor exists is never disclosed to the claimant — the copy says "the
current owner, or the vendor who manages this ScanStory for them", which is true
either way.

**Owner/vendor side.** The response section now shows the real
`response_deadline_at` and explains that after it the ScanStory team can review
directly. Visibility is still `user_can_respond_to_claim()`'s, untouched;
an unrelated account sees neither the deadline nor the response controls
(regression-tested).

Status constants were verified against `models.py:1033`-`models.py:1044` before the
presentation was built.

## 12. Accessibility / responsive considerations

- **No colour-only state meaning anywhere.** Every coverage chip pairs an icon
  with text; every admin badge prints the raw status *and* the human label; every
  non-active coverage state and every expired transfer also has a prose sentence.
- New async regions use `role="status"` `aria-live="polite"` (refund recovery
  message per row, claim-lookup result).
- The claim-lookup control is a real `<form>` with a real `<label>`,
  `aria-describedby` help text and a real submit button, so Enter works and
  focus/keyboard order is native. Focus moves to the evidence textarea when the
  claim form appears.
- No critical action is hover-only. The refund recovery button, the coverage grant
  and the claim form are all persistently visible and reachable by keyboard.
- Every mutating control keeps an explicit `confirm()` naming what does and does
  not change.
- All new tables sit inside the existing `.table-responsive` wrapper (the P1A
  overflow work), so the new worklist and out-of-band tables scroll horizontally
  on narrow screens instead of overflowing the page.
- The coverage warning is a flow paragraph inside the existing card, so it reflows
  at the card's own breakpoints; the Ownership Center's existing 620px
  single-column rule already covers the new sections' `handover-form` grid.
- Copy stays in the existing voice ("ScanStory", "handover", "review request") and
  no new visual language, font, animation or component class was introduced.

## 13. Focused tests and exact counts

New file `tests/integration/test_v11_final_ui_completion.py` — **36 passed**.

| Area | Tests |
|---|---|
| F1 refund attention / recovery worklist | 8 |
| F2 project coverage badges / warnings | 7 |
| F3 admin coverage grant control | 4 |
| F4 claimant discovery / claim entry | 6 |
| F5 transfer expiry / EXPIRED presentation | 5 |
| F6 vendor-awaiting-response presentation | 6 |

Existing-lane regression checks (focused and scoped — the full suite was **not**
run, per lane policy):

| Suite(s) | Result |
|---|---|
| `test_v1_agent2_admin_parity.py`, `test_wave4_vendor_ownership_backend.py`, `test_wave5_admin_commercial_completion.py` | **114 passed** (13m19s) |
| `test_v11_p1_backend_security_ops.py`, `test_user_projects_page.py`, `test_admin_refunds.py`, `test_v11_p0_refund_recovery.py` | **97 passed** (11m07s) |

**Total: 247 passed, 0 failed** (36 new + 211 existing). No PostgreSQL certification was attempted: no migration
was added in this lane and Alembic head is unchanged at `c1a7f3d95e24`.

Five of the new tests failed on their first run and all five were fixed rather
than weakened: a CSS attribute selector polluted a `data-coverage-warning`
assertion (selector changed to a class), a code comment in the claim-lookup script
contained the literal `ALREADY_OPEN` and so leaked a raw enum into the page
(reworded — a real finding, caught by its own test), and three tests asserted
against wrong premises (`RazorpayWebhookEvent` has no `event_id`/`payload_json`
column because the raw provider body is deliberately never persisted; and the
coverage-grant page's inert script lookup mentions the form id regardless of
permission, so the absence assertion now targets the actual control).

## 14. `git diff --check`

Clean — exit 0, no whitespace errors.

## 15. Scanner / runtime untouched confirmation

`git hash-object` at the lane start commit, at the sync base, and at the end:

| File | `eff937e` | `fb02d4d` | End |
|---|---|---|---|
| `scanner_runtime.py` | `5fdc3ff81e356cfad5c4896f0f9ec6bc9c6bf989` | `5fdc3ff81e356cfad5c4896f0f9ec6bc9c6bf989` | `5fdc3ff81e356cfad5c4896f0f9ec6bc9c6bf989` |
| `static/js/scanner-runtime.js` | `b14a4ca6eb9619f81ddee1eb00075c15b1fd64a4` | `b14a4ca6eb9619f81ddee1eb00075c15b1fd64a4` | `b14a4ca6eb9619f81ddee1eb00075c15b1fd64a4` |

Byte-identical. Neither file appears in this lane's diff. No OpenCV / ORB /
RANSAC / homography / optical-flow / calibration / geometry code was read or
changed, and `templates/user/scanner.html` was deliberately not modified (§9).

## 16. Integration worktree untouched confirmation

`F:\ScanStory-main\ScanStory-integration` was **never accessed** in this lane — not
read, not written, not staged, not committed, not merged. The sync used the local
object database (`git merge fb02d4d` inside this worktree only) and needed no
access to that directory. Nothing from this lane was merged into integration.

The audit file `SCANSTORY_V1_1_PRODUCTION_READINESS_AUDIT.md` is genuinely absent
from this worktree (confirmed by `ls`), consistent with Agent 1's note that it
exists only as an untracked file in the integration worktree. §17 therefore works
from the P1 report's own restatement of the still-open findings plus direct code
verification, rather than fabricating access to it.

## 17. Remaining HIGH findings by category

Source for the still-open set: `V1_1_P1_BACKEND_SECURITY_OPS_REPORT.md` §20 (which
listed 5 as STILL OPEN — FRONTEND and 2 as STILL OPEN — OPERATIONS/DEPLOYMENT).
Each was re-verified against current code, and `V1_1_P1A_FRONTEND_UX_HARDENING_REPORT.md`
was read for the P1A side.

| # | Finding | Classification | Verification |
|---|---|---|---|
| PAY-2 | No admin UI for the refund `needs_attention` worklist | **RESOLVED IN THIS FINAL UI LANE** | worklist on `admin/operations.html` driven by `stuck_refund_query()`, out-of-band block, confirm-gated recovery, manual review withheld; 8 tests (§6) |
| OWN-2 | Claim submission surfaced only to managing vendors | **RESOLVED IN THIS FINAL UI LANE** | claimant entry point in the Ownership Center on the existing `claim-lookup` endpoint, opaque non-eligible handling, no new enumeration surface; 6 tests (§9) |
| COV-1 | Project list shows no coverage state | **RESOLVED IN THIS FINAL UI LANE** | four-state badge from `coverage_summary.coverage_state`, suspended kept distinct; 7 tests (§7) |
| COV-2 | No per-project coverage-expiry warning | **RESOLVED IN THIS FINAL UI LANE** | per-state warning copy plus the real end date for active coverage; the near-expiry *threshold* variant is deferred for lack of a truthful backend signal (§19) |
| COV-3 | Admin coverage-grant endpoint has no UI control | **RESOLVED IN P1A** (not in P1, and not newly here) | the form has posted to the real endpoint since `4ee2993`; Agent 1's STILL-OPEN classification was already stale (§18). This lane completed its explanatory copy and state display only; 4 tests (§8) |
| SEC-3 | Razorpay credentials never validated at startup | **STILL OPEN — OPERATIONS/DEPLOYMENT** | `_validate_required_runtime_config()` (`app.py:141`) still says "Does not validate payment credentials"; `RAZORPAY_KEY_ID`/`_SECRET`/`RAZORPAY_WEBHOOK_SECRET` absent from the production-required list. Not a frontend item |
| SEC-4 | CSP ships report-only by default | **STILL OPEN — OPERATIONS/DEPLOYMENT** | `CSP_ENFORCE = _env_flag("SECURITY_CSP_ENFORCE", default=False)` at `app.py:616`, unchanged; the in-code comment still requires real-device QA before flipping it |

**Totals after this lane:**

| Category | Count |
|---|---|
| STILL OPEN — FRONTEND | **0** |
| STILL OPEN — OPERATIONS/DEPLOYMENT | **2** (SEC-3, SEC-4) |
| STILL OPEN — BACKEND | **0** |
| RESOLVED IN THIS FINAL UI LANE | 4 |
| RESOLVED IN P1A | 1 (COV-3) + the 2 Agent 1 already credited (OWN-1, RSP-1) |
| DEFERRED WITH REASON | 0 HIGH findings (one sub-variant of COV-2 — a near-expiry threshold — is deferred, see §19) |

No MEDIUM, LOW or POLISH item was fixed opportunistically. The one adjacent
correction made was a single sentence on the operations page that P1-3 had turned
into a false statement ("worker count is not reported by this backend yet"); it now
points at `/ready`'s `checks.workers` / `usable_worker_count` instead. No data or
logic was added for it.

## 18. Backend contract discrepancies found

1. **COV-3 was misclassified as STILL OPEN — FRONTEND.** `V1_1_P1_BACKEND_SECURITY_OPS_REPORT.md`
   §20 and §21.4 state that "zero templates post to
   `/admin/projects/<id>/service-coverage/grant`". A permission-gated, CSRF-bearing
   form with the correct `days` and `reason` field names has existed in
   `templates/admin/view_project.html` since P1A commit `4ee2993`, which is an
   ancestor of Agent 1's own starting point `5808d5b`. Code is authoritative; the
   report's prose was stale. No functional impact — the finding was already closed.
2. **`existing_claim_id` is absent, not `null`, in the `NOT_CLAIMABLE` body.** The
   P1 report's §15 example shows `existing_claim_id` as a response field. The real
   `not_eligible` dict (`app.py:7557`) contains only `success`, `eligible`,
   `reason_code`, `reason`, `project`, `claim_url` — the key is missing entirely
   rather than `None`. The frontend therefore never reads it on a non-eligible
   answer. Harmless, but worth recording for any future client.
3. **`RazorpayWebhookEvent` has no raw-payload column.** The P1 report's "no
   provider payload" guarantee for the out-of-band block is stronger than stated:
   the model persists only a `payload_hash` fingerprint and deliberately never the
   body (`models.py` class docstring), so there is no payload that *could* leak.
   Two of the new tests were rewritten around this once discovered.
4. **`coverage_state` collapses "suspended and also expired" into `suspended`.**
   Correct and deliberate per `project_coverage_state()`'s docstring, but it means
   a UI cannot show both facts at once, and must not try to reconstruct the second
   from raw dates. This lane shows `suspended` only. Noted, not changed.
5. **No near-expiry field exists** in `project_coverage_summary()`. Confirmed
   against code, matching the report's own statement that no days-remaining field
   was added. See §19.

No backend capability turned out to be missing, so no sub-item was stopped.

## 19. Known limitations

1. **No near-expiry ("expiring soon") state.** There is no truthful backend signal
   for it, and computing a threshold in Jinja would duplicate coverage rules. The
   badge shows the real `effective_coverage_until` for active coverage instead. A
   backend `days_remaining` / `expires_soon` field is the correct next step.
2. **The public scanner page has no claim link.** `templates/user/scanner.html` is
   a full-screen AR camera surface with no static footer, and this lane must not
   perturb scanner behaviour. A claimant therefore reaches the entry point via the
   Ownership Center (linked from Dashboard, My Stories and Profile) rather than
   from the scan itself. Adding a link there is a small, separate, scanner-QA'd
   change.
3. **Claimant discovery is still bounded by the existing `/scanner/<id>` disclosure.**
   Unchanged from Agent 1's P1-10 residual constraint: a share-token reference
   format would be the real fix and does not exist in this codebase.
4. **The refund worklist is capped at 50 rows** and has no pagination or filter.
   `/admin/api/refunds?needs_attention=1` remains the paginated read for a deeply
   backlogged install, and `flask reconcile-refunds` remains the bulk operator
   path. Pagination is worth adding only if a real deployment exceeds the cap.
5. **The expired-handover list is capped at 25 rows** and is not searchable. It is
   a "what happened to my handover" answer, not an archive.
6. **Recovery outcome is reported from one call.** Reconciliation can still settle
   later via webhook, so the page reloads and re-renders the server's authoritative
   status rather than treating the POST response as final — same policy as the
   existing add-on refund control.
7. **`admin_operations()` now performs two extra queries per page load** (the
   attention query and the out-of-band query). Both are indexed reads on bounded
   result sets on an admin-only page.
8. **`/admin/operations` requires `superadmin.operations.view`**, so an admin with
   only `admin.payments.view` cannot reach the worklist even though the panel is
   separately gated on that permission. Widening the page's own permission was out
   of scope for a UI lane.
9. **No browser or real-device QA was performed.** All verification here is
   server-rendered assertions plus code reading. Real-device QA of these screens,
   and of SEC-4's CSP enforcement, remains outstanding.
10. **Full suite not run.** Focused lanes only, per policy. The authoritative full
    regression is the human's step.

## 20. Final verdict

All six UI areas are complete and consume the P1 backend contracts as they
actually exist in code, verified field name by field name. No coverage, refund,
expiry or claim-governance rule is recomputed in a template or in JavaScript: each
screen branches on a backend-provided string and formats a backend-provided
timestamp. Every mutating control is confirm-gated, CSRF-bearing and withheld
wherever the backend would refuse it — including the two cases that matter most,
`MANUAL_REVIEW_REQUIRED` refunds and vendor-managed claims awaiting a vendor
response. Zero HIGH findings remain classified FRONTEND. The two that remain
(SEC-3 Razorpay startup validation, SEC-4 CSP enforcement) are
operations/deployment configuration decisions outside any UI lane.

**FINAL UI COMPLETE — READY FOR AUTHORITATIVE REGRESSION**
