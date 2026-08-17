# ScanStory V1.1 — P2 Browser / Device / Accessibility Certification Report

**Lane:** P2 — Browser, device and accessibility certification (final checkpoint of the V1.1 release series)
**Worktree:** `F:\ScanStory-main\ScanStory-v1.1-agent2`
**Branch:** `agent/v1.1-experience-ux`
**Lane type:** CERTIFICATION (inspect / run / interact / classify). Not an implementation lane.
**Date:** 2026-08-17 → 2026-08-18

---

## 1. Starting HEAD

`f55407bdeeffc006d168db2a97d7c2f9a08af261` — "final UI completion tip".

Pre-sync verification performed in this lane before any other action:

```
git -c safe.directory="F:/ScanStory-main/ScanStory-v1.1-agent2" status --short   -> (empty, clean)
git -c safe.directory="F:/ScanStory-main/ScanStory-v1.1-agent2" rev-parse --abbrev-ref HEAD -> agent/v1.1-experience-ux
git -c safe.directory="F:/ScanStory-main/ScanStory-v1.1-agent2" rev-parse HEAD  -> f55407bdeeffc006d168db2a97d7c2f9a08af261
```

No tracked modifications existed before sync, so the sync proceeded as authorised.

## 2. Integration HEAD and synced HEAD

- **Authoritative integration HEAD:** `55622c0b7edbd670c9162734ac9ca21a274ef64d`
  (`Merge branch 'agent/v1.1-platform-admin' into develop/scanstory-v1.1`)
- **Sync result:** clean **fast-forward**, no merge commit, **no conflicts**.
- **Synced HEAD:** `55622c0b7edbd670c9162734ac9ca21a274ef64d`

Files brought in by the sync (10 files, +1009 / −121):

```
.env.example                                        |  17 +-
V1_1_FINAL_SECURITY_DEPLOYMENT_REPORT.md            | 470 +++++++++++
app.py                                              |  84 +++-
docs/production/README.md                           |   4 +-
docs/production/deployment-runbook.md               | 159 ++++--
docs/production/monitoring-alerting.md              | 122 +++---
tests/conftest.py                                   |  27 ++
tests/integration/test_final_runtime_database_hardening.py |  3 +
tests/security/test_runtime_hardening_p0.py         |   8 +
tests/security/test_v11_final_security_deployment.py| 236 +++++++
```

Untracked local-only artefacts were observed in the worktree and **not touched**.

## 3. Ending HEAD

`a7da99c0a553ea847aef67082e004c5d2b615ed7`

## 4. Commits made in this lane

One commit:

| SHA | Subject |
|---|---|
| `a7da99c` | `fix(v1.1): register Razorpay payment.failed on the checkout instance` |

## 5. Files changed

| File | Change |
|---|---|
| `templates/user/subscribe.html` | +12 / −6 — frontend only |
| `V1_1_P2_BROWSER_DEVICE_ACCESSIBILITY_CERTIFICATION_REPORT.md` | this report (new) |

No backend, model, migration, config, payment-logic, ownership-logic, coverage-logic, storage-accounting or scanner-runtime file was modified.

---

## 6. Test environment actually used

Reused the pattern established by prior checkpoints in this project rather than inventing a new one.

- **Python:** `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe` (authoritative; never bare `python`).
- **App startup:** scoped local dev server on **`http://127.0.0.1:5050`**, launched from a scratchpad harness that mirrors `tests/conftest.py::isolated_app` — `SCANSTORY_TESTING=1`, `TEST_DATABASE_URL=sqlite:///…/certstate/cert.db`, isolated `SCANSTORY_DATA_DIR` / `SCANSTORY_ADMIN_DATA_DIR` / `SCANSTORY_STATIC_UPLOADS_DIR`, `FLASK_ENV=development`, `SCANSTORY_DEV_TESTING=1`, `SCANSTORY_QUEUE_MODE=fake`, `FLASK_DEBUG=0`.
- **Deliberate divergence from the unit-test fixture:** CSRF was left **enabled** (`WTF_CSRF_ENABLED=True`) and CSP was set to **enforced** (`SECURITY_CSP_ENABLED=1`, `SECURITY_CSP_ENFORCE=1`) because those are the two things this lane exists to certify in a real browser. The unit fixture disables CSRF; this lane must not.
- **Authentication:** real login form POSTs through the browser as synthetic seeded users. **No password reset was performed on any real/local account**, and no signed cookie was hand-minted — the real login flow was exercised instead, which also certifies U1.
- **Database usable:** confirmed — bootstrap produced 3 subscription plans and 1 bootstrap admin; all seeding and reads succeeded.
- **Redis/RQ:** not required by any flow certified here; queue mode `fake`. `/admin/operations` correctly reports "Queue availability check: Unknown / not verified", "Mode fake", "Redis configuration: Not configured".
- **Razorpay:** **no keys configured** in this environment (`razorpay_client` is falsy; startup logs "Razorpay keys not configured"). **No real or test payment was ever initiated.** Payment surfaces were certified for rendering, wiring and messaging only.
- **Secrets:** no DB / SMTP / Razorpay / webhook secret, Flask secret, connection string or password hash was printed, logged or screenshotted. An automated secret-leak regex sweep was additionally run over every captured page body (§34).

### Browser automation method

Playwright's Python package was already present in this session's scratchpad and was used to drive the **real installed Chrome and Edge** via `channel="chrome"` / `channel="msedge"`. No browser binaries were downloaded and nothing was installed into the authoritative venv (a single pure-Python helper was installed into a scratchpad-local `--target` directory only). This is equivalent to the CDP-direct approach prior checkpoints used, with the same real-browser guarantee.

### Synthetic data seeded

All-synthetic, `*.example.test` identities, password `CertLane123!`, bootstrap admin password from the harness env only:

- 6 users: owner, claimant, vendor, peer, lapsed-subscription, never-covered
- 9 projects covering **all four** `coverage_state` values and **all three** legal experience/playback pairings
- 7 ownership transfers covering **all six** `PROJECT_TRANSFER_STATUSES`
- 9 ownership claims covering **all nine** `PROJECT_CLAIM_STATUSES`
- 5 refunds spanning `status` × `reconciliation_status`, including one settled (`REFUNDED`+`APPLIED`) control that must be excluded from the attention worklist

---

## 7. Browsers actually tested

| Browser | Available | Tested | Notes |
|---|---|---|---|
| Google Chrome (real install) | Yes | **Yes — primary** | Full matrix: all 7 viewports, all journeys, scanner |
| Microsoft Edge (real install) | Yes | **Yes — cross-check** | 9 critical pages × 2 viewports + scanner; reproduced every finding |
| Firefox | **No** — not installed | No | Verified absent at `C:\Program Files\Mozilla Firefox\firefox.exe`; not fabricated |
| Safari / iOS Safari | **No** — not available on win32 | No | Documented as NOT EXECUTED (§38) |

Both tested browsers are Chromium-family. This is stated honestly rather than presenting Edge as independent engine coverage — **no Gecko or WebKit engine was certified in this lane.**

## 8. Viewports actually tested

All seven required viewports were executed: **1440x900, 1280x720, 1024x768, 768x1024, 430x932, 390x844, 360x800**.

Mobile widths (<500px) were run with `is_mobile=True` and `has_touch=True`. An eighth, **844x390**, was additionally used for the scanner landscape-orientation test.

## 9. Devices / hardware tested

- **No physical mobile device** was used. Mobile results are emulated viewport + touch + mobile-UA, not device hardware.
- **No real camera hardware.** Scanner camera paths used Chrome's synthetic capture device (`--use-fake-device-for-media-stream`). See §39.
- No real QR code was physically scanned; no printed marker was presented to a lens.

---

## 10. U1 — AUTH / ACCOUNT — **PASS**

Certified: landing, register, login, forgot-password, reset-password, verify-email, terms, privacy, contact, pricing, logout, session-expiry.

| Check | Result |
|---|---|
| All auth pages return 200 | PASS |
| Login works in real Chrome and Edge | PASS → redirects to `/dashboard` |
| Admin login works | PASS → `/admin/dashboard` |
| Logout | PASS → returns to `/` |
| Protected page while logged out | PASS → `/login/`, **no redirect loop** |
| Session dropped mid-session (cookies cleared) | PASS → graceful `/login/`, no loop, no 500 |
| Bad-password feedback | PASS → generic **"INVALID EMAIL OR PASSWORD"** (no user enumeration) |
| Password field masked (`type=password`) | PASS |
| Submitted password never echoed into DOM | PASS |
| Form labels present on all auth forms | PASS — 0 unlabeled visible controls |
| CSRF token present on every auth POST form | PASS |
| Visible focus ring on every tab stop | PASS |
| Submit button reachable/visible at all 7 viewports | PASS |
| Horizontal overflow on auth pages | PASS at all widths except the decorative-blob issue on landing (§25, MEDIUM) |

## 11. U2 — USER DASHBOARD — **PASS (1 MEDIUM)**

- `/dashboard` and `/profile` load 200 at all 7 viewports; `h1`=1; `main` landmark present on dashboard.
- Subscription / entitlement info readable; 6 status badges all carry **visible text** (never colour-only).
- Ownership navigation present and reachable; mobile nav collapses to a working menu toggle.
- No duplicate or dead navigation on the user side.
- **MEDIUM:** decorative `.blob` gradient divs are not clipped, producing ~12–30px of unintended horizontal page scroll at 390x844, 360x800, 430x932 and 1024x768 (§25).

## 12. U3 — PROJECT LIST — **PASS** (strongest result in the lane)

All four backend `coverage_state` values were reproduced and verified **in a real browser**:

| `coverage_state` | Rendered label | Verified |
|---|---|---|
| `active` | "Coverage active" | PASS (incl. "· no end date" for the indefinite case) |
| `expired` | "Coverage expired" | PASS |
| `none` | "No coverage" + `data-coverage-warning="none"` | PASS |
| `suspended` | **"Suspended by ScanStory"** | PASS |

- **Suspended is kept visually and textually distinct from expired** — the locked rule from prior waves holds. Regex-verified that the suspended project is never labelled expired.
- **No client-side business-rule calculation.** `templates/user/projects.html` branches *only* on the backend string `project.coverage_summary.coverage_state` (lines 657/664/669/674/704/707/711) and renders labels from the backend-provided `PROJECT_COVERAGE_STATE_LABELS` dict. No day-math, no date arithmetic, no "N days left" text anywhere in the template or output. The P1 backend contract is intact.
- Coverage date formatting readable; indefinite/no-end-date case handled explicitly.
- Transfer/ownership context and role labels present.
- Verified across all 7 viewports — **no overflow, no clipped card, no offscreen action** on `/projects` at any width.

*Note:* the `none` state required stripping the trial that login auto-provisions; an earlier apparent mismatch was my own stale seed state, not an application defect — the UI matched the resolver exactly once re-measured.

## 13. U4 — PROJECT CREATE — **PASS (1 MEDIUM)**

- `/create-project` loads 200 at all 7 viewports; keyboard-usable; no clipped controls; no offscreen submit.
- **Only the three locked combinations are presentable.** Backend guard `_validate_project_experience_playback` (app.py:3989) permits exactly `direct_qr`+`direct` and `image_video`+{`tracked_overlay`,`detect_once`}; the creator surfaces exactly the matching labels.
- `tracked_overlay` is labelled **"Tracked Overlay"**. **"Object Tracking" appears nowhere** — verified by string search over the rendered page. PASS on the locked naming rule.
- Invalid combinations are not encouraged by the UI.
- CSRF is carried by the `X-CSRFToken` header on the AJAX/resumable upload path (`csrfHeader()`, and the XHR at line 4561), which is the real submission mechanism — the absence of a hidden `csrf_token` input in the `<form>` is therefore not a gap.
- **MEDIUM:** the two file selectors (`images`, `videos`) have **no accessible label** (no `for=`, `aria-label`, or wrapping `<label>`) — the only unlabeled visible controls on the user-side critical path.

## 14. U5 — PROJECT EDIT / PREVIEW — **PASS (1 MEDIUM)**

- Edit certified for an `image_video` project and a `direct_qr` project; preview certified for tracked-overlay, detect-once and suspended projects. All 200.
- Suspended project preview correctly reports the suspended state, not expiry.
- Ownership context (creator / current owner / managing vendor / beneficiary) renders where applicable.
- Save/cancel reachable and visible at 390x844 and 360x800.
- Public-availability warning copy present.
- **MEDIUM:** `project_preview` pages have **no `h1`** (the only pages in the whole sweep missing one) — heading structure starts below `h1`.
- A `404 /qr/project_1_main.png` on `/success/1` is a **seed artefact** (the synthetic project has no generated QR image file), not an application defect.

## 15. U6 — OWNERSHIP CENTER — **PASS (1 documented by-design scope limit)**

Verified against the **actual** current constants (not the assumed ones):

- Transfer statuses are `PROJECT_TRANSFER_STATUSES` on **`ProjectOwnershipTransfer`**; expiry field is **`expires_at`**.
- Claim statuses are `PROJECT_CLAIM_STATUSES` on **`ProjectOwnershipClaim`**; the deadline field is **`response_deadline_at`**, *not* `expires_at`. (Corrects the brief's assumption.)

| Item | Result |
|---|---|
| **All 9 claim status labels render** | PASS — every one of the nine confirmed on-screen |
| Transfer labels rendered | `Waiting for recipient`, `Recipient needs project/storage capacity`, `Transfer under review`, `Transfer expired` — PASS |
| `COMPLETED` / `CANCELLED` transfers in user ownership centre | **Not shown — by design** (see below) |
| Expired transfer offers no Accept/Reject | PASS — expired transfers are listed separately precisely so a terminal state cannot inherit an action control |
| `expires_at` / deadline displayed | PASS |
| Ownership-unchanged copy | PASS |
| Linked claim separately visible | PASS |
| Claimant copy never implies ownership already moved | **PASS** — no "you now own" / "is now yours" / "transferred to you" phrasing at any review stage; review requirement is stated |

**By design, not a defect:** `/ownership` (app.py:7366–7389) scopes incoming/outgoing to `PROJECT_ACTIVE_TRANSFER_STATUSES` plus a separate `EXPIRED` list. The route's own comment states the intent: the lists "deliberately mean 'still actionable'", and `EXPIRED` was added separately "so the terminal state cannot inherit an action control." A completed handover remains visible to the claimant through the claim record (`TRANSFER_COMPLETED` → "Ownership handed over") and to staff through `/admin/ownership`, which lists all transfers unfiltered. Surfacing completed/cancelled transfer history to the user would require changing the route query in `app.py` — outside this lane's permitted fix areas — so it is **recorded, not changed**. Classified LOW.

## 16. U7 — CLAIM DISCOVERY — **PASS** (anti-enumeration verified)

`GET /api/ownership/claim-lookup/<int:project_id>` exercised through `fetch()` inside a real authenticated browser session:

| Scenario | HTTP | `eligible` | `reason_code` | `project` |
|---|---|---|---|---|
| Non-existent id (99999) | 200 | `false` | `NOT_CLAIMABLE` | `null` |
| Suspended / unavailable project | 200 | `false` | `NOT_CLAIMABLE` | `null` |
| Current owner looking up own project | 200 | `false` | `NOT_CLAIMABLE` | `null` |
| Claimant, duplicate active claim | 200 | `false` | `ALREADY_OPEN` | present |

- **The locked anti-enumeration property holds.** A non-existent project id and a real-but-not-claimable project return **byte-identical** responses — same status, same `reason_code`, same `project: null`. No differentiation leaks project existence.
- **Owner/manager is not encouraged to self-claim** — the owner's own project reports `NOT_CLAIMABLE` with no project data and no claim URL.
- **Duplicate active claim gets safe copy** (`ALREADY_OPEN`), not a second submission path.
- Submission clearly states review is required; the vendor-first / admin path is described accurately.
- **No new sequential-ID browsing surface** was introduced: the endpoint is `@login_required`, rate-limited (bucket `ownership_claim_lookup`, 429 + `Retry-After`), and returns the same opaque body for every non-eligible case.

## 17. U8 — PAYMENT / ADD-ON UI — **PASS after the HIGH fix (§21)**

- `/subscribe` (plans + add-ons + capacity/storage purchasing) and `/pricing` load 200 at all 7 viewports; `/payment-success` 200.
- **No real Razorpay keys configured; no payment was initiated.** Verified `razorpay_client` falsy.
- Pricing/currency readable; mobile cards usable at 360x800; no offscreen purchase button at any width.
- **No raw provider payload and no secret values rendered** (regex sweep, §34).
- **RELEASE-RELEVANT DEFECT FOUND AND FIXED:** uncaught `TypeError: Razorpay.on is not a function` on every load of both routes — see §21.
- **Gap (not a defect of this lane's scope):** there is **no user-facing payment-history route or template**. Payment history exists only in the admin view (`admin/view_user.html`). The brief's U8 "payment history" item therefore has **no user surface to certify** — recorded as a product gap, not a defect.
- `/payment-failed` has no template (it redirects to `/subscribe`), so failure messaging is delivered through the in-page modal — which is exactly what the §21 defect had broken.

---

## 18. A1 — ADMIN LOGIN / NAV — **PASS (1 LOW)**

- Admin login 200, works in Chrome and Edge; dashboard 200.
- 17 sidebar links; **0 offscreen at desktop**; active nav state marked (`aria-current`/`.active`).
- Responsive collapse works: 14 visible links at 768x1024 → 1 + working menu toggle at 390x844 and 360x800. No inaccessible offscreen nav.
- Permission gating drives visible actions (super-admin session used; permission-gated routes reachable).
- **LOW:** one dead `href="#"` link, and two duplicated nav targets (`/admin/dashboard`, `/admin/settings`) — the dashboard duplicate is the logo + nav item, which is conventional.

## 19. A2 — USERS — **PASS (1 MEDIUM)**

- `/admin/users`, `/admin/users/1`, `/admin/users/1/dashboard`, `/admin/scans`, `/admin/scans/user/1`, `/admin/capacity` all 200.
- Subscription / entitlements / account type / capacity / scanner-lock surfaces render.
- **Table overflow handled correctly:** every admin table has `<th>` headers and sits inside an `overflow-x` scroll container, so rows scroll within their wrapper instead of breaking the page. At 390x844 and 360x800 `/admin/users` shows **no page-level horizontal overflow** (`scrollWidth == clientWidth`). Earlier raw flags for this page were my detector counting in-scroll-container buttons and were retracted on deep probe.
- **MEDIUM:** `admin/view_user.html` has 4 unlabeled visible controls (`storage_bytes`, `project_slots`, and two `reason` fields) — these are entitlement-mutation inputs, so labels matter here.

## 20. A3 — PROJECT ADMIN — **PASS** (governance contracts all hold)

Certified across four project states (active / expired / suspended / vendor-managed):

| Check | Result |
|---|---|
| Project list + project view load | PASS (200) |
| Suspend / reactivate (`/suspend`, `/restore`) present, POST + CSRF | PASS |
| **No revoke button anywhere** | **PASS** — regex `\brevoke\b` matched **zero** times on all four project views, matching the backend (no revoke capability exists) |
| Governed coverage-grant form reachable | **PASS** — Grant + days + reason all present on `admin/view_project.html` |
| Confirmation before mutation | PASS |
| Permission-gated visibility (`superadmin.capacity.manage`) | PASS |
| **Copy separates project service coverage from account subscription** | **PASS** — "service coverage" wording present and distinct |
| Suspended view says suspended, never expired | PASS |
| Coverage status/history + ownership link | PASS |
| `Coverage State` rendered from backend string only | PASS — `data-coverage-state` + `PROJECT_COVERAGE_STATE_LABELS` lookup, no template math |

`/admin/project/1/preview` returning **404 is correct behaviour, not a defect**: that route is scoped to admin-*owned* projects (`if project.owner_admin_id != admin.id: abort(404)`, app.py:14441) and project 1 is user-owned.

## 21. A4 — PAYMENTS / REFUNDS — **PASS** (and the lane's HIGH fix)

### Refund attention / recovery worklist — verbatim evidence

`/admin/operations` rendered **"Refunds Needing Attention — 4 open"**. All four qualifying refunds appeared and the settled control was correctly excluded:

| Seeded refund | `status` | `reconciliation_status` | In worklist | Operator action offered |
|---|---|---|---|---|
| `rfnd_cert_1` | `REFUND_FAILED` | `FAILED` | Yes | "Recover / reconcile" |
| `rfnd_cert_2` | `REFUND_PROCESSING` | `PENDING` | Yes | "Recover / reconcile" |
| `rfnd_cert_3` | `REFUNDED` | **`MANUAL_REVIEW_REQUIRED`** | Yes | **"No automatic action available."** |
| `rfnd_cert_4` | `REFUND_REQUESTED` | `PENDING` | Yes | "Recover / reconcile" |
| `rfnd_cert_5` | `REFUNDED` | `APPLIED` (settled) | **No — correctly excluded** | n/a |

- **`MANUAL_REVIEW_REQUIRED` has NO misleading auto-fix button.** The exact rendered copy is: *"Manual decision required. This is not automatically fixable: an admin has to reconcile the entitlement by hand. Retrying will not resolve it."* followed by *"No automatic action available."* This is still correct.
- Recovery copy is accurate and non-destructive: *"Re-reads the provider on this same record. No second refund is issued."* Page-level copy adds that recovery *"never issues a second refund, and never deletes a ScanStory, its media or its QR code."*
- Settled-exclusion copy is explicit: *"Settled refunds are not listed."*
- **Safe messages only** — only `failure_message_safe` / `reconciliation_message_safe` surfaced. **No raw provider payload, no secrets.**
- POST + CSRF intact on mutation controls; confirmation required before mutation.
- `/admin/payments`, `/admin/payments/1`, `/admin/webhook-events`, `/admin/subscriptions` all 200; webhook history link present.

### The HIGH defect found and fixed

**`templates/user/subscribe.html:1496` called `Razorpay.on('payment.failed', …)` — a static method that does not exist.**

Reproduction (Chrome and Edge, `/subscribe` and `/pricing`, all viewports): uncaught `TypeError: Razorpay.on is not a function` on **every** page load.

Root cause proven, not guessed: I loaded the live Razorpay checkout SDK on a blank page with **no CSP applied at all** and enumerated it — static keys are `['sendMessage','emi','configure','defaults','enableLite','setConfig','open','triggerShopifyCheckoutBtnClickEvent','showLoader']`; `Razorpay.on` is `undefined`, while `'on' in Razorpay.prototype` is `true`. So `on` is an **instance** event API and the static call could never have worked. This ruled out the CSP-blocked `cdn.razorpay.com` bundle as the cause.

User-visible impact:
1. The `payment.failed` handler never registered → **a failed payment left the modal on the "Processing payment…" spinner with no error message and no "Try Again" button.** Misleading status on a payment surface.
2. The throw aborted the rest of the `DOMContentLoaded` handler → the modal body-scroll-lock `MutationObserver` was never installed.

Fix (minimal, frontend only, in an allowed area): registered `payment.failed` on the `rzp` **instance** inside `subscribeToPlan()` — the documented API — and removed the impossible static call. **No payment, pricing, entitlement or refund rule was touched.**

Verification: zero page errors on `/subscribe` and `/pricing` in **Chrome and Edge** at 1440x900, 768x1024, 390x844 and 360x800; focused tests re-run green (§32).

## 22. A5 — OWNERSHIP REVIEW — **PASS** (gating verified against the real backend condition)

Rather than eyeballing the UI, I verified the rendered controls against the actual gating function `claim_admin_review_block_reason(claim, now=None)` (app.py:2498).

| Claim row | Status | Vendor | Block reason returns | Controls rendered |
|---|---|---|---|---|
| #1 | `VENDOR_NOTIFIED` | yes, deadline future | **blocked** | **none** — no approve/reject |
| #2 | `OPEN` | no vendor | `None` | Approve, Reject |
| #3 | `PENDING_ADMIN_REVIEW` | no vendor | `None` | Approve, Reject |
| #4 | `APPROVED_BY_VENDOR` | — | `None` | Approve, Reject |

- **Vendor-blocked rows exposing approve/reject: 0.** Admin controls are hidden exactly when the backend would block adjudication.
- The block reason is surfaced **verbatim**: *"This project has a managing vendor, who has not responded yet. Admin review opens once the vendor responds or the vendor response deadline passes."*
- **Direct-admin path still works** for no-vendor claims (3 rows with working Approve/Reject).
- **After vendor response, admin actions appear** (`APPROVED_BY_VENDOR` row has controls).
- **Terminal claims carry no invalid mutation controls** — `REJECTED`, `CANCELLED`, `EXPIRED`, `TRANSFER_COMPLETED`, `APPROVED_BY_ADMIN` rows expose none.
- Transfer rows: active → Complete/Dispute/Cancel; `DISPUTED` → Release dispute/Cancel; `EXPIRED`/`COMPLETED`/`CANCELLED` → **no mutation controls**. Expiry and pending-capacity states display correctly.

## 23. A6 — OPERATIONS — **PASS**

- `/admin/operations` 200 under `superadmin.operations.view`; also certified `/admin/settings`, `/admin/activity-logs`, `/admin/plans`, `/admin/addons`, `/admin/moderation`, `/admin/admins`.
- Readiness/queue info presented honestly and without overclaiming: *"Configured Redis/RQ does not prove a worker is online. The live worker count is reported by the /ready probe (checks.workers / usable_worker_count), not by this page."* Likewise for SMTP: *"Configured SMTP does not prove email delivery."*
- Refund attention state correct (§21). Worker state presentation accurate for `fake` queue mode.
- **No raw infrastructure secrets** — SMTP host/user shown as "Not configured", no credentials, no connection strings.
- Mobile table overflow handled via scroll containers; 7 tables on the page, all with `<th>`.
- **MEDIUM:** `/admin/plans` has real horizontal overflow at desktop (§25).

---

## 24. Scanner browser certification — **RELEASE BLOCKER FOUND**

Scanner internals were **never modified**; this was visual/runtime exercise only (§36).

| Item | Result |
|---|---|
| Scanner page loads | PASS — 200 for all three playback modes at 4 viewports |
| Intro state | **PASS** — clean, readable, "Follow the target" + "Start Camera" CTA, no overflow (screenshot captured) |
| Loading state | PASS |
| Project-unavailable state | **PASS** — suspended project returns 404 with correct copy: *"This experience is unavailable — The project you're looking for is currently suspended or unavailable."* Correctly says suspended/unavailable, never "expired" |
| Camera permission UX | PASS (synthetic device) |
| **Camera denial handling** | **PASS** — hard-denied `getUserMedia` (`NotAllowedError`); denial is surfaced to the user, no unhandled crash |
| Back button | PASS — present and functional |
| Video element behaviour | PASS — elements present, sized, inside viewport; `objectFit: cover` |
| Overlay visual containment | PASS — 3 canvases, all inside viewport at every tested width |
| Orientation change | **PASS** — portrait 390x844 → landscape 844x390 → back: **no horizontal overflow in any state**, no new JS error attributable to rotation |
| Tab visibility / background-return | PASS — survived backgrounding and return with no overflow and no new error |
| Runaway layout overflow | **PASS** — none at 360x800, 390x844, 768x1024, 1440x900 |
| **CSP-blocked required scanner resource** | **FAIL — RELEASE BLOCKER (§28)** |
| `direct_qr` playback | **PASS** — zero JS errors, zero CSP violations, at all 4 viewports; correctly never fetches `opencv.js` |
| `detect_once` experience | **FAIL — blocked (§28)** |
| `tracked_overlay` experience | **FAIL — blocked (§28)** |

## 25. Responsive / mobile result — **PASS with 2 MEDIUM**

All 7 viewports × 12 user pages × 12 admin pages executed on Chrome, plus a 2-viewport × 9-page Edge cross-check.

**Clean:** `/projects`, `/ownership`, `/subscribe`, `/create-project`, `/admin/operations`, `/admin/ownership`, `/admin/users`, `/admin/projects`, `/admin/view-project`, `/admin/payments`, `/admin/capacity`, `/admin/settings`, `/admin/dashboard` — no page-level horizontal overflow, no offscreen action, no clipped content, no overlapping text, no sticky/fixed collision, readable badges, usable touch targets, alerts/toasts visible.

Two real defects, both **MEDIUM** and therefore **deliberately not fixed** in this certification lane:

**M1 — `/admin/plans` horizontal overflow at desktop.**
At 1440x900 `scrollWidth=1534` vs `clientWidth=1440` (94px). The plan **lifecycle `Set` form and the `Delete` button** sit at `left=1479..1534`, fully outside the viewport. Reproduced on **Edge** at 1440x900. Not present at 360x800 (responsive stacking takes over). The controls remain reachable by horizontal page scroll, so this is a recoverable layout defect rather than an inaccessible admin action — MEDIUM, not HIGH.

**M2 — decorative `.blob` divs cause unintended mobile horizontal scroll.**
Unclipped absolutely-positioned gradient blobs (`blob w-96 h-96`, `blob w-[500px]`) extend past the viewport on `/` and `/dashboard`: 425 vs 390 (landing @390x844), 402 vs 390 (dashboard @390x844), 1054 vs 1024 (dashboard @1024x768). Reproduced on Edge. Purely decorative — no content or control is lost — but the page can be dragged sideways. `/projects` proves the correct pattern: its blobs overflow their box but the container clips them, so `scrollWidth == clientWidth`.

Retracted false positives (honesty note): the first responsive pass measured 320ms after `domcontentloaded` and flagged `login`, `admin-users`, `admin-ownership`, `admin-operations`, `admin-view-project` and `admin-payments`. A deep re-probe at 500ms showed `scrollWidth == clientWidth` for all of them — the flags were mid-flight AOS scroll-animation transforms and in-scroll-container table buttons, i.e. **intended behaviour**. These are reported as clean above.

## 26. Accessibility smoke — **PASS with 3 MEDIUM**

Manual keyboard/visual inspection plus scripted DOM auditing (no new accessibility framework was installed, per the brief). 15 critical screens.

| Criterion | Result |
|---|---|
| Document title exists | **PASS — 15/15**, and on every one of the 54 captured pages |
| Exactly one `h1` | PASS on 15/15 critical screens; **fails only on `project_preview`** (§14) |
| `lang` attribute | PASS — `lang="en"` on all |
| Form controls have labels | PASS on 11/15; **4 pages have unlabeled visible controls** (below) |
| Required fields identifiable | PASS — `required`/`aria-required` present |
| **Keyboard Tab order usable** | **PASS** — 13–16 reachable stops per page in logical order, no keyboard trap |
| **Visible focus indicator** | **PASS — 0 tab stops without a visible focus ring on any of the 15 screens** |
| Buttons are buttons / links are links | PASS — no `div[onclick]`/`span[onclick]` misuse found |
| Mutation controls keyboard-accessible | PASS — all POST forms have real `<button>`/`<input type=submit>`; 0 forms without a submit control |
| Modals/confirmations keyboard-usable | PASS |
| **Status not communicated by colour alone** | **PASS** — badges carry visible text; 1 exception on `admin-view-project` (1 of 2 badges) |
| Badges have visible text | PASS (1 exception above) |
| Tables retain readable header context | **PASS — 0 tables without `<th>`** across all admin tables |
| No hover-only critical action | PASS — `group-hover` used only for decorative emphasis |
| Touch targets reasonable on mobile | PASS on critical paths |
| Images have alt where meaningful | PASS |
| Obvious contrast failure | None observed in visual inspection of captured screenshots |
| CSRF on POST forms | PASS — 0 missing (the single `create-project` flag is header-based, §13) |

MEDIUM accessibility findings:
- **A11Y-1:** `project_preview` pages have no `h1` (§14).
- **A11Y-2:** unlabeled visible form controls on 4 pages — `create-project` (2 file inputs), `admin/view_user` (4 entitlement-mutation inputs), `admin/plans` (3 lifecycle selects), `admin/addons` (4 inputs), plus `contact` (4).
- **A11Y-3:** no `main` landmark on 8 of 15 screens (`login`, `register`, `forgot-password`, `create-project`, `subscribe`, `admin-login`, `admin-users`, `admin-projects`, `admin-view-project`), and **no skip-to-content link anywhere**.

LOW: 1 badge without text on `admin-view-project`; 9 tab stops on `admin-ownership` measured outside the viewport at the instant of focus (browser scroll-into-view makes this largely cosmetic; not reproduced as a usability failure).

## 27. Keyboard navigation result — **PASS**

Explicit 16-step `Tab` walks from a blurred, scroll-reset document on all 15 critical screens.

- 13–16 focusable stops reached per screen, in DOM/visual order.
- **Every single reached stop had a visible focus ring** (outline width > 0 or a box-shadow) — **0 failures across all 15 screens**. This is the strongest accessibility result in the lane.
- No keyboard trap; no focus lost to `body`; every mutation control (suspend, restore, coverage grant, refund recover, claim approve/reject, transfer complete/dispute/cancel) is a real focusable button.

## 28. Console / network / CSP result — **1 RELEASE BLOCKER, otherwise clean**

Inspected via the browser's own console/network/pageerror channels on every certified page.

### RELEASE BLOCKER — B1: production CSP blocks OpenCV.js, breaking two of three scanner modes

**Reproduction:** load `/scanner/<id>` for an `image_video` project (either `tracked_overlay` or `detect_once`) with `SECURITY_CSP_ENFORCE=1` (the production posture).

**Captured stack — this is the decisive evidence:**

```
EvalError: Evaluating a string as JavaScript violates the following
Content Security Policy directive because 'unsafe-eval' is not an allowed
source of script: script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' ...
    at new Function (<anonymous>)
    at createNamedFunction (http://127.0.0.1:5050/static/js/opencv.js:30:10849388)
    at extendError        (http://127.0.0.1:5050/static/js/opencv.js:30:10849580)
    at                     http://127.0.0.1:5050/static/js/opencv.js:30:10912389
```

**Root cause:** `static/js/opencv.js` (self-hosted Emscripten glue) calls `new Function(...)` during module initialisation. The production CSP `script-src` grants `'wasm-unsafe-eval'`, which permits WebAssembly compilation but **not** `new Function` on a JS string. OpenCV therefore never initialises and `window.cv` stays `undefined`.

**Causation proven by counterfactual**, so this is not an artefact of my harness:

| CSP mode | `typeof window.cv` | `cv.Mat` ready | Scanner outcome |
|---|---|---|---|
| `Content-Security-Policy-Report-Only` | `object` | **true** | tracked_overlay and detect_once both fine, 0 errors |
| `Content-Security-Policy` (enforced) | `undefined` | false | both modes fail |

**User-visible impact — two distinct failure modes, both confirmed by screenshot after pressing "Start Camera":**

1. **`tracked_overlay`** degrades to a fallback video and tells the user: **"Camera is unavailable on this device. You can still view the fallback video."** This message is **misleading** — the camera is available and working (the sibling mode obtained a live stream, `readyState=4`). The user is directed to blame their device. The AR tracked-overlay experience — the product's core value — does not run. The error state also renders with visual damage: the top panel is clipped against the header and the same message appears twice (a card and a banner).
2. **`detect_once`** obtains a live camera stream and then **hangs permanently** on a **"Waiting for OpenCV…"** status chip with "Looking for image…", scanning forever and never recognising anything. No fallback, no failure message, no recovery.

3. **`direct_qr` is unaffected** — it never loads `opencv.js` by design, and showed 0 errors and 0 CSP violations at every viewport.

**Cross-browser:** reproduced identically on **Microsoft Edge**. Not browser-specific.

**Why this ships broken rather than being caught in dev:** `CSP_ENFORCE` defaults to `_runtime_production_mode_flag_active()` (app.py:670), so local development runs **report-only** and the scanner works. Production startup validation *requires* `SECURITY_CSP_ENFORCE=1` (app.py:165–166). The failure therefore appears **only in production**.

**NOT FIXED — deliberately.** The fix belongs in the CSP `script-src` definition in `app.py` (~line 679). `app.py` is outside this lane's permitted fix areas, and weakening a security policy is explicitly a security-rule change this lane must not make. Per the fix policy this is a **NO-GO item, reported rather than implemented.** Adding `'unsafe-eval'` also has real security cost and should be a deliberate decision by the security/backend owner — alternatives worth their consideration include a CSP hash/nonce strategy, or an OpenCV build that does not require `new Function`.

### Everything else — clean

| Check | Result |
|---|---|
| Console errors (application) | **None** after the §21 fix |
| Uncaught exceptions | **None** after the §21 fix |
| Failed JS loads (application) | None |
| 404 static assets (application) | None reproducible |
| Mixed content | None |
| Application 4xx/5xx during navigation | None (the only 404s are intentional: suspended-project scanner, admin-owned-only preview) |
| Security headers | Present on every response: `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-Frame-Options: SAMEORIGIN`, `Permissions-Policy: camera=(self), microphone=(), geolocation=()`, plus enforced `Content-Security-Policy` |
| reCAPTCHA asset loads | No failures observed |

## 29. Third-party resource observations (classified separately)

This sandbox **has** working internet access (Razorpay checkout assets returned HTTP 200), so third-party failures here are real, not sandbox artefacts.

- **T1 (LOW, third-party):** `https://cdn.razorpay.com/static/cx/razorpay-risk-detection/bundle.js` is **CSP-blocked** on `/subscribe`, `/pricing`, `/profile` and project-preview pages. The Razorpay allowlist covers `checkout.razorpay.com` but not `cdn.razorpay.com`, which checkout loads into the top-level document. Checkout itself initialises (`window.Razorpay` is a function) and its iframe assets load normally, so this degrades Razorpay's fraud/risk telemetry rather than payment capability. Now **non-fatal** — it was previously compounded by the §21 defect. Fixing it would require an `app.py` CSP change → reported, not fixed.
- **T2 (LOW):** `http://127.0.0.1:5050/media/demo` reports `net::ERR_ABORTED` on the landing page — a media element aborting its own fetch, with no HTTP error status. Benign media negotiation; no user-visible impact.
- **T3 (informational):** `https://checkout-static-next.razorpay.com/build/undefined` → `ERR_BLOCKED_BY_ORB`, originating inside Razorpay's own iframe. Third-party, not ours.
- One non-reproducible `404` console line was seen on `/register/` in the first pass and did **not** recur on a clean re-check with full `load` waiting. Not counted as a defect.

## 30. Screenshot / evidence captured

24 screenshots, representative rather than exhaustive, all from synthetic accounts and data. **No secret, card datum, SMTP/DB credential or private token appears in any of them.**

Desktop: user projects (incl. a dedicated no-coverage capture), dashboard, create-project, ownership, subscribe, admin dashboard, admin operations, admin ownership (plus a gating-specific capture), admin payments, admin project view.
Mobile: 390x844 projects, ownership, subscribe, admin operations, admin ownership; 360x800 admin plans and admin dashboard.
Scanner: tracked-overlay intro (390x844), direct-QR (390x844), unavailable/suspended state, camera-denied state, landscape 844x390, and post-"Start Camera" captures for all three playback modes (the blocker evidence).

## 31. Fixes made

Exactly one, and only because it met every condition in the fix policy (reproducible, release-relevant, purely presentation-layer, narrow, no backend/business/security rule touched, no scanner internals, focused verification).

**`templates/user/subscribe.html`** — register Razorpay `payment.failed` on the checkout **instance** instead of the non-existent static `Razorpay.on`. Detail and verification in §21.

No MEDIUM, LOW or POLISH finding was fixed, per the lane's fix policy.

## 32. Automated tests run

Only the narrowly-relevant suites for the one changed template — the full 1945-test regression was **not** re-run, as instructed.

```
python -m pytest -q \
  tests/gate_jr/test_v11_commercial_ownership_ux.py \
  tests/gate_jr/test_v11_admin_refund_ux.py \
  tests/integration/test_payment_and_admin_baseline.py \
  tests/integration/test_v11_final_ui_completion.py

=> 124 passed, 0 failed, 798 warnings in 407.91s
```

No PostgreSQL certification was re-run (no migration involved). Warnings are pre-existing SQLAlchemy 2.0 `Query.get()` legacy notices, unrelated to this change.

## 33. Known untested / not-executed surfaces

Declared honestly rather than claimed:

- **Real physical devices** — none. All mobile results are emulated.
- **Real camera hardware, physical markers, printed QR codes** — none (§39).
- **Firefox / Gecko** and **Safari / WebKit** — no engine coverage (§38).
- **A real payment, refund or add-on purchase** — never executed; no Razorpay keys configured.
- **Live Redis/RQ worker behaviour** — queue mode `fake`; worker liveness is a `/ready` probe concern, not certified here.
- **Real SMTP delivery / live email OTP round-trip** — SMTP unconfigured; the verify-email *UI* was certified, delivery was not.
- **User-facing payment history** — no such route/template exists to certify (§17).
- **`/admin/moderation` report-review mutations, plan/add-on CRUD write paths, admin password reset** — pages loaded and inspected, write paths not exercised.
- **Upload of real media through the resumable pipeline** — projects were seeded directly; the create-project *form* was certified, an end-to-end large-file upload was not.
- **`430x932` deep-probe** — included in the responsive matrix but not in the 500ms deep re-probe set.
- **Automated axe-core style scanning** — not installed and deliberately not added; accessibility findings come from scripted DOM auditing plus manual keyboard/visual inspection.

## 34. Secret-leak verification

An automated regex sweep over **every captured page body** (54 page captures across user and admin sweeps) for `rzp_live_`/`rzp_test_`, `sk_live`, `password_hash`, `pbkdf2:`, `scrypt:`, `postgresql://`, `SMTP_PASS`, `RAZORPAY_KEY_SECRET`, `FLASK_SECRET_KEY`, `whsec_`, and the harness secret value:

**0 hits.** No secret is rendered anywhere in the UI. `/admin/operations` shows configuration *presence* ("Not configured") without values.

## 35. Migration status

**No migration was created, run or modified.** `git status --short migrations/` is empty. Alembic head remains `c1a7f3d95e24`. Exactly as required.

## 36. Scanner freeze verification

`scanner_runtime.py` and `static/js/scanner-runtime.js` were **read but never modified**. No OpenCV/ORB/RANSAC/homography/optical-flow/geometry/threshold/calibration/detection-math/overlay-positioning/watchdog logic was touched.

| File | SHA256 before | SHA256 after | Identical |
|---|---|---|---|
| `scanner_runtime.py` | `a092b3f141f4e1ca743e45693db5b3560843b86baf59b853570607174982af16` | `a092b3f141f4e1ca743e45693db5b3560843b86baf59b853570607174982af16` | **YES** |
| `static/js/scanner-runtime.js` | `95d5305dd3f8c1c0d1db84ca90b51fe79b8bb322bf1b1a2a3e771c270b3eb7b3` | `95d5305dd3f8c1c0d1db84ca90b51fe79b8bb322bf1b1a2a3e771c270b3eb7b3` | **YES** |

Byte-identical. Freeze honoured.

## 37. Final repository checks

```
git diff --check      -> clean (no whitespace/conflict errors)
git status --short     -> (empty; the one fix is committed as a7da99c)
git diff --stat        -> (empty)
migrations/            -> unchanged
```

**Integration worktree untouched:** `F:\ScanStory-main\ScanStory-integration` HEAD is still `55622c0b7edbd670c9162734ac9ca21a274ef64d` with **no tracked modifications**. It was used as read-only reference only. No published V1 branch or tag was touched. Nothing was pushed; integration was not merged into.

## 38. Safari / iOS status

**NOT EXECUTED.** Safari and iOS Safari are unavailable on this win32 host — there is no WebKit engine and no iOS simulator reachable here. This is **not** claimed, faked or inferred, matching how prior checkpoints in this project handled the same limitation. **WebKit remains uncertified for V1.1** and should be covered on real Apple hardware before or during staging — particularly the scanner, which is the most engine-sensitive surface (iOS Safari has its own `getUserMedia`, autoplay, inline-video and WASM behaviour).

## 39. Real camera / hardware status

**NOT HARDWARE-CERTIFIED.** No physical camera exists on this host. All camera paths used Chrome's synthetic capture device. Specifically **not** certified: real sensor image quality, autofocus/exposure behaviour, real marker recognition accuracy, tracking stability under motion, target loss and re-acquisition with a physical marker, real-device thermal/performance behaviour, and physical QR scanning.

What **was** legitimately certified: page load, intro state, permission-grant and hard-denial handling, video/canvas element presence, sizing and viewport containment, overlay containment, orientation change, background/return, project-unavailable state, and — critically — the CSP/JS runtime failure in §28, which is engine-level and entirely independent of camera hardware.

---

## 40. Defects found — complete list with severity

| ID | Sev | Area | Defect | Status |
|---|---|---|---|---|
| **B1** | **RELEASE BLOCKER** | Scanner / CSP | Production CSP `script-src` lacks `'unsafe-eval'`; `static/js/opencv.js` needs `new Function`. `tracked_overlay` shows a misleading "Camera is unavailable on this device" and falls back to video only; `detect_once` hangs forever on "Waiting for OpenCV…". `direct_qr` unaffected. Reproduced on Chrome **and** Edge. Manifests **only** in production (dev is report-only). | **NOT FIXED** — fix requires `app.py` CSP/security config, outside permitted fix areas → NO-GO, reported |
| **H1** | HIGH | Payment UI | `Razorpay.on(...)` static call does not exist → uncaught `TypeError` on every `/subscribe` and `/pricing` load; `payment.failed` never registered, so a failed payment left a perpetual "Processing payment…" spinner with no error and no retry | **FIXED** (`a7da99c`), verified Chrome + Edge × 4 viewports, 124 focused tests green |
| M1 | MEDIUM | Admin layout | `/admin/plans` horizontal overflow at 1440x900 / 1280x720 (1534 vs 1440); lifecycle `Set` form and `Delete` button fully offscreen but reachable by page scroll. Reproduced on Edge | Not fixed (policy) |
| M2 | MEDIUM | Responsive | Unclipped decorative `.blob` divs cause unintended horizontal page scroll on `/` and `/dashboard` at 390x844, 360x800, 430x932, 1024x768, 768x1024 | Not fixed (policy) |
| M3 | MEDIUM | Accessibility | `project_preview` pages have no `h1` | Not fixed (policy) |
| M4 | MEDIUM | Accessibility | Unlabeled visible form controls: `create-project` (2 file inputs), `admin/view_user` (4 entitlement-mutation inputs), `admin/plans` (3), `admin/addons` (4), `contact` (4) | Not fixed (policy) |
| M5 | MEDIUM | Accessibility | No `main` landmark on 8 of 15 critical screens; no skip-to-content link anywhere | Not fixed (policy) |
| L1 | LOW | Admin nav | One dead `href="#"` link; duplicate nav targets (`/admin/dashboard`, `/admin/settings`) | Not fixed (policy) |
| L2 | LOW | Accessibility | One badge without visible text on `admin-view-project` | Not fixed (policy) |
| L3 | LOW | Accessibility | 9 tab stops on `admin-ownership` measured outside viewport at focus instant (browser scroll-into-view mitigates) | Not fixed (policy) |
| L4 | LOW | Ownership UX | `COMPLETED` / `CANCELLED` transfers absent from user `/ownership` — **by design** per the route's own comment; visible via claim record and admin console | Not fixed (by design; would need `app.py`) |
| L5 | LOW | Third-party | `cdn.razorpay.com` risk-detection bundle CSP-blocked; degrades Razorpay fraud telemetry, not payment capability | Not fixed (needs `app.py` CSP) |
| L6 | LOW | Media | `/media/demo` `ERR_ABORTED` on landing; benign, no HTTP error | Not fixed (policy) |
| L7 | LOW | Product gap | No user-facing payment-history route or template exists | Recorded, not a regression |

## 41. Remaining counts per severity tier

| Tier | Found | Fixed | **Remaining** |
|---|---|---|---|
| **RELEASE BLOCKER** | 1 | 0 | **1** |
| **HIGH** | 1 | 1 | **0** |
| **MEDIUM** | 5 | 0 | **5** |
| **LOW / POLISH** | 7 | 0 | **7** |

## 42. What this lane confirms is healthy

Worth stating plainly, because the blocker is narrow and the rest is genuinely strong:

- Every certified route returns 200; **no 500 anywhere**; no broken redirect loop.
- **All four `coverage_state` values** render correctly, with suspended kept distinct from expired, and templates branching **only** on backend strings — the P1 contract holds.
- **All nine claim statuses** and the transfer statuses render with correct, truthful labels.
- **Claim anti-enumeration holds** — non-existent and non-claimable projects are indistinguishable.
- **Admin claim adjudication gating exactly matches `claim_admin_review_block_reason()`** — zero blocked rows expose approve/reject.
- **Refund attention worklist is correct in every respect** — all four qualifying states present, settled excluded, `MANUAL_REVIEW_REQUIRED` offers no misleading auto-fix, all copy safe.
- **No revoke UI** exists for coverage, matching the backend.
- **Zero secrets** rendered anywhere.
- **Zero tab stops without a visible focus ring** across 15 critical screens.
- **Zero tables without `<th>`**; all admin tables scroll inside their own container.
- CSRF and the full security header set present on every response.
- `direct_qr` scanner mode is fully clean.
- Only three of the three legal experience/playback pairings are presentable, and `tracked_overlay` is never called "Object Tracking".

## 43. Staging recommendation

**Do not promote to staging as an unconditional V1.1 candidate until B1 is resolved.**

B1 is narrow, precisely diagnosed and cheap to fix — but it disables the AR experience for **two of three playback modes**, and only in the production CSP posture, which is exactly where it would first be seen by real users. Its user-facing symptom actively misdirects diagnosis by blaming the viewer's device.

Recommended sequence:

1. **Resolve B1 in a backend/security-owned lane** (not this one): decide between adding `'unsafe-eval'` to `script-src`, adopting a hash/nonce strategy, or shipping an OpenCV build that avoids `new Function`. This is a deliberate security trade-off and needs the security owner's sign-off.
2. **Re-certify the scanner with `SECURITY_CSP_ENFORCE=1`** for `tracked_overlay` and `detect_once` — confirm `window.cv` initialises and no `EvalError` is raised. Add a regression test asserting the scanner initialises under the *enforced* policy, so this cannot silently return; the current test posture did not catch it because dev runs report-only.
3. Then staging is reasonable, carrying M1–M5 and L1–L7 as documented non-blocking debt.
4. **During staging, cover the two genuine hardware/engine gaps:** real iOS Safari (§38) and real camera + physical marker scanning (§39). Neither could be executed here and both are material for an AR product.
5. Optionally schedule M3–M5 (accessibility: missing `h1`, unlabeled controls, landmarks/skip link) as a small follow-up — they are cheap and all sit in already-permitted template areas.

## 44. Fix-policy compliance statement

- No product file was changed on account of any MEDIUM, LOW or POLISH finding.
- The single change made was a HIGH, reproducible, presentation-layer defect on a critical journey, fixed minimally within permitted areas (`templates/user/*`) and verified by focused tests plus re-running the exact browser flow at neighbouring viewports in two browsers.
- `models.py`, `migrations/**`, `core/config.py`, `processing_queue.py`, payment/refund business logic, ownership transitions, coverage rules, storage accounting and scanner runtime internals were **not** modified.
- Where a defect required touching those areas (B1, L4, L5), the verdict on that item is **NO-GO and reported**, never worked around.

## 45. Honesty and scope statement

Nothing in this report is asserted without evidence actually captured in this lane. Specifically:

- Firefox, Safari, iOS, real devices, real cameras, physical markers and real payments are reported as **not executed**, not as passes.
- Both tested browsers are Chromium-family; Edge is reported as a cross-check, not as independent engine coverage.
- Six pages initially flagged for overflow were **retracted** after deeper measurement showed the flags were measurement races against scroll animations and intended in-container scrolling.
- One apparent `coverage_state` mismatch was traced to my own stale seed state and retracted as an application defect.
- The Razorpay CSP block was investigated and explicitly **ruled out** as the cause of H1 before H1 was attributed to the template.
- Journeys not reached are listed in §33 rather than presented as passes.

## 46. Final verdict

The scanner is the product. Two of its three playback modes do not function under the CSP posture production enforces, and the failure tells the user their device is at fault. That is a release-relevant defect, it is still present, and by this lane's own rules it cannot be fixed here.

**P2 CERTIFICATION FAILED — RELEASE-RELEVANT DEFECT REMAINS**

*This report certifies browser, viewport and accessibility behaviour only, on emulated viewports with a synthetic camera, in two Chromium-family browsers. It does not claim production readiness.*
