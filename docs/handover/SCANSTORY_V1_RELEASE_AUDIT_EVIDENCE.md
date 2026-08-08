# ScanStory V1 — Final Release Audit & MNC Handover Evidence Pack

**Repository:** `ScanStory-integration` (repository root)
**Branch:** `hardening/saas-v1-production`
**HEAD:** `cfe959b7bf00a87d9c2c1c74ab1fe178c67e52eb` (confirmed via `git -c safe.directory='<repo root>' log -1`)
**Working tree:** 69 uncommitted entries at audit start (`git status --short`), preserved exactly, not staged, not committed, not tagged during this audit.
**Prior release state:** LOCAL DEV GO (final regression pass + a P1 nav-link closure already completed before this audit began).
**Audit method:** Read-only static inspection (Grep/Glob/Read/targeted single-test runs only) plus synthesis of prior audit documents. No product file was modified. No migration was applied. No secret value is reproduced anywhere in this document.
**Prior documents used as an index (not as ground truth — re-verified against current code where cited):**
- `<sibling repo ScanStory-main>/SCANSTORY_SAAS_AUDIT.md` (old HEAD `1918e75`)
- `<sibling repo ScanStory-main>/SCANSTORY_V1_REPOSITORY_LINEAGE_AUDIT.md` (baseline `79fea11`)
- `<sibling repo ScanStory-main>/SCANSTORY_V1_FEATURE_PARITY_AUDIT.md` (HEAD `cfe959b` — **same HEAD as this audit**, treated as current except where the dirty batch visibly supersedes it)
- `SCANSTORY_V1_GAP_AUDIT.md` (old HEAD `c0ba483`, read-only, untouched)
- `project-understanding/FINAL-PROJECT-UNDERSTANDING.md` (early snapshot, pre-dates Redis/RQ/resumable-upload/webhook work)
- `docs/production/*.md` (9 files — an already-existing, high-quality, current production-operations doc set; used extensively as primary source for Parts L–P below, cited by filename throughout)

---

## How to read this document

Every major finding carries:
- **VERIFIED FACT** — the claim.
- **EVIDENCE/SOURCE** — exact file/route/model/config-key/command/test-file/migration/template.
- **OPERATIONAL IMPLICATION** — what it means for whoever runs this in production.
- **HANDOVER NOTE** — what the MNC handover package needs to say about it.
- **STATUS** — one of `VERIFIED`, `ASSUMPTION`, `TBD`, `DEFERRED_V1`, `SERVER_TEAM_VERIFY`.

No secret values, passwords, hashes, tokens, or credentials appear anywhere below — only env var **names** and safe example **formats**.

---

# PART A — Executive Summary (filled in after all parts; see PART Y for the formal verdict)

This audit reconstructs the full current state of ScanStory V1 at HEAD `cfe959b` plus its 69-file uncommitted release batch, cross-checking three prior audits and one still-open gap audit against the actual current code, and builds the factual evidence base for the eventual MNC handover package (23 documents, blueprinted in PART S).

Headline: the product's customer-paid workflow, payment idempotency/webhook reconciliation, capacity enforcement, RQ-based background processing, and resumable uploads have all materially matured since the older audit documents were written — most of the P0/P1 items those documents flagged as open are now closed with dedicated migrations, models, and CLI tooling. The main remaining gaps are honestly-disclosed **external verification gaps** (real-device scanner testing, live Razorpay webhook delivery to a real HTTPS staging endpoint, SMTP-in-anger) that are explicitly the domain of a staging/server team, not of this repository's code.

(Full verdict: PART Y. Full risk register: PART X.)

---

# PART B — Product / V1 Scope Audit

## B.1 Product purpose

ScanStory is a Flask web application that lets a **creator** upload one or more reference-image + video pairs ("a Project"), receive a QR code per project, and have any **anonymous viewer** who scans that QR code see the matching video overlaid on their live camera feed once the reference image is recognized (client-side OpenCV.js optical-flow tracking + server-side ORB/homography detection). Source: `project-understanding/FINAL-PROJECT-UNDERSTANDING.md` §1, re-confirmed structurally current by this audit's own route/model reads.

## B.2 Core workflows (V1 INCLUDED)

| Workflow | Status | Evidence |
|---|---|---|
| Registration, email OTP verification, login, password reset | V1 INCLUDED | `app.py` auth routes; `models.py` `OTPCode`/`UserLoginActivity` |
| Project ("Memory") creation, image/video upload, QR generation | V1 INCLUDED | `app.py` `/create-project`, `/upload`; resumable upload API (see PART H) |
| Background feature-extraction processing (RQ-based) | V1 INCLUDED | `processing_jobs.py`, `processing_worker.py`, `rq_worker.py`, migration `a73f2c19d8e2_processing_job_rq_foundation.py` |
| Public AR scanner (anonymous) | V1 INCLUDED | `/scanner/<project_id>`, `/detect_init`, `/detect_track`, `/api/scanner/session/end` |
| Fallback experience when scanner/camera/OpenCV fails | V1 INCLUDED | scanner fallback panel, `templates/user/project_unavailable.html` (new in this release batch — see PART D/J) |
| Subscription plans, Razorpay checkout, webhook reconciliation | V1 INCLUDED | `PaymentOrder`, `PaymentReservation`, `CapacityConfig`, `RazorpayWebhookEvent`; migrations `54a108a17fa7`, `bc5642a86981`, `ebeab1cf4ec9` |
| Hard capacity gate (paid-account cap, default 25) | V1 INCLUDED | `SCANSTORY_INITIAL_CAPACITY_LIMIT` (see `docs/production/README.md`); capacity reservation lifecycle |
| Admin panel (users, projects, plans, subscriptions, payments, webhook events, capacity, admins, settings, activity logs) | V1 INCLUDED | `templates/admin/*`; see PART D/J/T |
| Account lifecycle (block/unblock, trial→paid, expiry) | V1 INCLUDED | `User.has_active_subscription()`/`can_create_project`/`can_scan` (per `SCANSTORY_SAAS_AUDIT.md` §5, re-verified structurally present) |

## B.3 V1 DEFERRED

- **Experience Creator subsystem** (`experience_creator.py`, `publishing.py`, `Organization`/`Workspace`/`WorkspaceMember`/`Experience`/`Trigger` models, `templates/user/experiences/*`) — fully built, dormant, feature-flag-gated off. **Verified this audit**: `feature_flags.py` (lines 4–13) defines exactly 8 flags — `ENABLE_EXPERIENCE_CREATOR`, `ENABLE_TRIGGER_MANAGEMENT`, `ENABLE_PROCESSING_STATUS_UI`, `ENABLE_EXPERIENCE_QR_ASSET`, `ENABLE_EXPERIENCE_PUBLISHING`, `ENABLE_PUBLIC_EXPERIENCE_ROUTE`, `ENABLE_VERSION_ROLLBACK`, `ENABLE_EXPERIENCE_PAUSE` — **all hard-coded `False`** in the `EXPERIENCE_CREATOR_FLAGS` dict, only overridable per-process via env var (`flag_enabled()`, lines 19–25). No env var setting any of these to true was found in `.env.example`. STATUS: **VERIFIED — DEFERRED_V1**. This subsystem must be described only as deferred/dormant in every handover document, never as released functionality.
- Refunds — **confirmed absent**. No refund route, no Razorpay refund API call, no `status="refunded"` write path anywhere. `docs/production/README.md` "Known Operational Gaps" states plainly: "There is no automatic refund flow, and no refund/chargeback/settlement/subscription-renewal webhook event support — out of scope entirely." STATUS: VERIFIED — do not imply a refund feature exists anywhere in the handover pack.
- Subscription renewal/proration/recurring billing — deferred (one-time Razorpay Orders only, per prior audits, structurally unchanged).
- Storage byte-quota enforcement — deferred (bounded today only by the 25-account capacity cap).

## B.4 EXPERIMENTAL / FEATURE-FLAG-GATED

- Same list as B.3 — Experience Creator is the only flag-gated subsystem found in the repository.

## B.5 LOCAL QA ONLY

- `windows_rq_worker.py` — untracked, 5-line `SimpleWorker`/`TimerDeathPenalty` subclass, a Windows-only workaround for RQ's SIGALRM-based timeout not existing on native Windows. **Must never ship to staging/production** (Linux production must use the standard `rq_worker.py`/`rq worker` flow). See PART H.
- `instance/*.db` (multiple SQLite files, including files literally named `*_wave5_smoke_*`, `*_merge_check.db`, `*wave7_device_test.db`) — local developer/test database snapshots. See PART Q.
- `add_simple_admin.py`, `fix_limits.py`, `gate_*` directories, `migration_gate_c.py`, `gate_c_migration.py`, `gate_d_*` — local one-off dev/QA scripts from the hardening project's own history, not part of the shipped runtime.

## B.6 SERVER OPERATIONS ONLY

- Alembic migration execution, RQ worker process supervision, PostgreSQL/Redis provisioning, reverse proxy/TLS, backup execution, all `flask <cli-command>` reconciliation tooling — the responsibility of the server/infra team per this task's explicit boundary, documented in PARTS L–P.

---

# PART C — System Architecture Audit

## C.1 Component inventory (verified from source)

| Component | Evidence |
|---|---|
| Flask monolith | `app.py` (single large route/service module) |
| SQLAlchemy ORM | `models.py` |
| Alembic migrations | `migrations/` — `alembic.ini`, `env.py`, `versions/` (7 revisions — see PART E) |
| Production DB driver | `psycopg[binary]<=3.2.3` in `requirements.txt` (PostgreSQL); `pymysql<=1.1.1` also present (legacy/secondary driver — see PART R for disposition) |
| Redis + RQ queue | `redis<=5.0.8`, `rq<=1.16.2` in `requirements.txt`; `rq_worker.py`, `processing_jobs.py`, `processing_worker.py`, `processing_orchestration.py`, `processing_queue.py`, `processing_readiness.py`, `processing_operations.py` |
| Resumable upload backend | `upload_validation.py`, `storage.py`, migration `44340c16353c_resumable_upload_sessions.py` |
| Scanner client runtime | `templates/user/scanner.html`, `static/js/scanner-runtime.js`, OpenCV.js/WASM under `static/js/` |
| Server-side detection | ORB/homography detection functions inside `app.py` (`/detect_init`, `/detect_track`) — see PART I |
| Payment integration | `razorpay<=2.16.0`; order/verify/webhook routes in `app.py`; `PaymentOrder`/`PaymentReservation`/`CapacityConfig`/`RazorpayWebhookEvent` models |
| Email/SMTP | `smtplib`-based sending in `app.py` (env-driven `SMTP_HOST/PORT/USER/PASS`, `MAIL_FROM`) |
| Static/frontend build | `package.json` — Tailwind CLI (`tailwindcss` devDependency) building `static/css/tailwind.build.css`, `esbuild` bundling the `qrcode` npm package to `static/vendor/qrcode/qrcode.min.js`; landing/blog pages still additionally load Tailwind's CDN JIT build (per `SCANSTORY_V1_GAP_AUDIT.md` PF5, not re-verified line-for-line in this pass) |
| CLI operations | Numerous `@app.cli.command()` entries in `app.py` (quota/capacity/webhook/upload-session reconciliation — enumerated in PART H/G) |
| Health/readiness | `GET /healthz` (liveness), `GET /ready` (DB readiness) — contracts documented in `docs/production/monitoring-alerting.md` |
| Rate limiting | `rate_limit.py` — explicitly documented as **process-local only**, not shared across Gunicorn workers (`docs/production/README.md` "Known Operational Gaps") |
| Reverse proxy trust | `ProxyFix(x_for=1, x_proto=1, x_host=1)` (`docs/production/security-proxy-checklist.md`) |

## C.2 Architecture diagram (built from the above, not invented)

```
                                   ┌───────────────────────────┐
                                   │   Reverse Proxy (TLS)     │
                                   │  (server-team supplied)   │
                                   │  ProxyFix: x_for=1 hop    │
                                   └─────────────┬─────────────┘
                                                 │ HTTPS
                     ┌───────────────────────────┼───────────────────────────┐
                     │                    Flask app (app.py)                 │
                     │  Gunicorn WSGI workers, ProxyFix normalized IP        │
                     │                                                       │
                     │  ┌─────────────┐ ┌─────────────┐ ┌─────────────────┐  │
                     │  │ Public /    │ │ User /      │ │ Admin /         │  │
                     │  │ Auth routes │ │ Project /   │ │ Super-Admin     │  │
                     │  │             │ │ Upload /    │ │ routes          │  │
                     │  │             │ │ Scanner     │ │                 │  │
                     │  └─────────────┘ └─────────────┘ └─────────────────┘  │
                     │  ┌─────────────┐ ┌─────────────┐ ┌─────────────────┐  │
                     │  │ Payment:    │ │ Webhook:    │ │ Health:         │  │
                     │  │ order/verify│ │ /webhooks/  │ │ /healthz /ready │  │
                     │  │             │ │ razorpay    │ │                 │  │
                     │  └──────┬──────┘ └──────┬──────┘ └─────────────────┘  │
                     │         └───────┬────────┘                            │
                     │           activate_payment() (shared, idempotent)     │
                     └───────────────────────┬───────────────────────────────┘
                                              │ SQLAlchemy
                              ┌───────────────┴────────────────┐
                              │   PostgreSQL (production)      │
                              │   SQLite (dev/test only)       │
                              │   Alembic-migrated schema      │
                              └────────────────────────────────┘
                                              │
                     ┌────────────────────────┼────────────────────────┐
                     │                                                 │
             ┌───────┴────────┐                             ┌──────────┴──────────┐
             │  Redis          │  enqueue                    │  Local media/       │
             │  (queue mode:   │◄────────────────────────────┤  feature-artifact   │
             │  rq / fake /    │                              │  storage            │
             │  inline)        │                              │  (SCANSTORY_DATA_DIR│
             └───────┬────────┘                              │  / _ADMIN_DATA_DIR) │
                     │ dequeue                                └──────────┬──────────┘
             ┌───────┴─────────────┐                                    │
             │ RQ worker process   │  reads/writes ProjectPair,         │
             │ (rq_worker.py —     │  ProcessingJob rows; OpenCV        │
             │ production Linux    │  feature extraction, image/video   │
             │ only)               │  standardization                   │
             └──────────────────────┘◄──────────────────────────────────┘

     ┌───────────────────────────┐        ┌──────────────────────────────┐
     │  Public scanner (browser) │  HTTPS │  Razorpay (external)         │
     │  OpenCV.js + camera +     │◄──────►│  Order/checkout/webhook      │
     │  optical-flow tracking    │        │  (payment.captured events)   │
     └───────────────────────────┘        └──────────────────────────────┘

     ┌───────────────────────────┐
     │  SMTP provider (external) │  OTP / verification / reset emails
     └───────────────────────────┘
```

STATUS: VERIFIED (components); the diagram is descriptive of code-confirmed components only — no invented infrastructure (e.g. no CDN, no object storage, no managed cache layer is claimed because none was found in code).

## C.3 Notes not yet filled — pending research agent reports

Detailed queue-mode env vars, ProcessingJob lifecycle wiring, resumable-upload contract, scanner detection constants, full env-var register, and full route grouping are being verified by parallel research passes and will be appended to PARTS D/E/G/H/I/K below in this same document as they land.

---

# PART Q — Release Content Audit (69 uncommitted working-tree entries)

Full current list captured via `git -c safe.directory='<repo root>' status --short` at audit time (69 entries). Classified below. **No `git add` command has been run. No file has been staged.**

## Q.1 Classification key
- **A — INCLUDE IN RELEASE**: intentional product change, safe/expected to ship.
- **B — EXCLUDE LOCAL/GENERATED**: must never be committed (local DB, generated artifact, dev-only script, shell mishap).
- **C — REVIEW BEFORE COMMIT**: plausibly fine but needs a human decision (naming, scope, or content check) before staging.

## Q.2 Modified tracked files (` M`)

| Path | Class | Notes |
|---|---|---|
| `app.py` | A | Diff confirmed narrowly scoped: hunks touch `admin_page_size`, `login_required`, `user_profile`, `_project_readiness_summary`, `admin_scans`, `admin_user_scans`, `admin_settings` (227 insertions/123 deletions total). **Grepped the full diff for `ORB_MAX_DIM`/`RANSAC`/`MIN_INLIERS`/`MIN_GOOD_MATCHES`/`DETECT_MAX_DIM`/`MAX_INLIERS` — zero matches.** No scanner-algorithm touch in this batch. Consistent with "final regression + P1 nav-link closure" framing. |
| `static/css/tailwind.build.css` | A | Generated Tailwind build output — 1 line changed; regenerate via `npm run build:css` if any doubt, but the diff itself is a normal compiled-CSS delta tracked deliberately (this repo tracks the *built* CSS, not `node_modules`). |
| `static/js/nav.js` (new, see Q.3) | A | See Q.3 — same feature. |
| `templates/admin/activity_logs.html` | A | Part of admin nav-consolidation batch (see PART J). |
| `templates/admin/add_plan.html` | A | Admin UI polish batch. |
| `templates/admin/base.html` | A | Central to nav-consolidation fix — 274 lines changed; verify against PART J findings before commit but functionally in-scope. |
| `templates/admin/edit_plan.html` | A | Admin UI polish batch. |
| `templates/admin/forgot_password.html` | A | Admin UI polish batch. |
| `templates/admin/login.html` | A | Admin UI polish batch. |
| `templates/admin/manage_admins.html` | A | Admin UI polish batch. |
| `templates/admin/payments.html` | A | Admin UI polish batch. |
| `templates/admin/plans.html` | A | Admin UI polish batch. |
| `templates/admin/project_preview.html` | A | Admin UI polish batch. |
| `templates/admin/projects.html` | A | Admin UI polish batch. |
| `templates/admin/reset_password.html` | A | Admin UI polish batch. |
| `templates/admin/scans.html` | A | Admin UI polish batch. |
| `templates/admin/settings.html` | A | Admin UI polish batch. |
| `templates/admin/subscriptions.html` | A | Admin UI polish batch — relevant to the parity doc's "unreachable page" finding; verify nav fix reaches it (PART J). |
| `templates/admin/user_profiles.html` | C | Prior parity audit flagged this as a near-duplicate/legacy view of `admin/users.html`. Confirm in PART J whether this edit is cosmetic polish on a still-live route or polish on a page that should instead be retired — review before commit if the route itself is scheduled for retirement. |
| `templates/admin/user_scans.html` | A | Admin UI polish batch. |
| `templates/admin/users.html` | A | Admin UI polish batch. |
| `templates/admin/view_payment.html` | A | Admin UI polish batch. |
| `templates/admin/view_project.html` | A | Admin UI polish batch. |
| `templates/admin/view_user.html` | A | Admin UI polish batch. |
| `templates/user/blog.html` | A | User-facing visual-polish batch. |
| `templates/user/blog_articles/article.html` | A | User-facing visual-polish batch. |
| `templates/user/contact.html` | A | User-facing visual-polish batch. |
| `templates/user/dashboard.html` | A | User-facing visual-polish batch (124 lines changed). |
| `templates/user/edit_project.html` | A | User-facing visual-polish batch. |
| `templates/user/experiences/detail.html` | A | Deferred-feature template, still receives cosmetic polish for consistency; confirm it stays unreachable (flag-gated) — see PART J. |
| `templates/user/experiences/list.html` | A | Same as above. |
| `templates/user/experiences/new.html` | A | Same as above. |
| `templates/user/experiences/public_unavailable.html` | A | Same as above. |
| `templates/user/experiences/public_viewer.html` | A | Same as above. |
| `templates/user/experiences/trigger_new.html` | A | Same as above. |
| `templates/user/forgot_password.html` | A | User-facing visual-polish batch. |
| `templates/user/landing.html` | A | User-facing visual-polish batch. |
| `templates/user/login.html` | A | User-facing visual-polish batch. |
| `templates/user/payment_success.html` | A | User-facing visual-polish batch. |
| `templates/user/privacy_policy.html` | A | User-facing visual-polish batch. |
| `templates/user/profile.html` | A | User-facing visual-polish batch. |
| `templates/user/project_preview.html` | A | User-facing visual-polish batch. |
| `templates/user/projects.html` | A | User-facing visual-polish batch. |
| `templates/user/register.html` | A | User-facing visual-polish batch. |
| `templates/user/reset_password.html` | A | User-facing visual-polish batch. |
| `templates/user/scanner.html` | A | 63 lines changed. Grepped separately for scanner-constant changes — none found in the tracked-diff portion reviewed; full independent confirmation pending PART I research-agent report; flag for final cross-check before sign-off. |
| `templates/user/subscribe.html` | A | User-facing visual-polish batch. |
| `templates/user/success.html` | A | User-facing visual-polish batch. |
| `templates/user/terms.html` | A | User-facing visual-polish batch. |
| `templates/user/user_create_project.html` | A | User-facing visual-polish batch. |
| `templates/user/verify_email.html` | A | User-facing visual-polish batch. |
| `tests/integration/test_admin_navigation_routing.py` | A | Test updated alongside the nav-consolidation fix — expected co-change. |
| `tests/integration/test_admin_panel_repair.py` | A | Same. |
| `tests/integration/test_admin_projects_module.py` | A | Same. |
| `tests/integration/test_super_admin_authorization.py` | A | Same. |
| `tests/security/test_csrf_and_headers.py` | A | Same. |
| `tests/security/test_otp_security.py` | A | Same. |

## Q.3 New tracked-path additions (`A`, i.e. `git diff` shows them as newly added but already `git add`-staged-looking `A` status — verify before assuming; re-confirm exact git status letter at commit time) and untracked new files (`??`)

| Path | Git status | Class | Notes |
|---|---|---|---|
| `static/css/design-system.css` | `A` | A | New design-system stylesheet — part of the visual-polish batch. |
| `static/js/nav.js` | `A` | A | New shared nav script — this is very likely the fix for the parity audit's "two competing admin nav systems" / unreachable Subscriptions+Activity Logs finding. Confirm via PART J agent report before final sign-off, but content-wise this is a clear INCLUDE. |
| `templates/admin/capacity.html` | `??` | A | New — closes the parity audit's P1 "no admin capacity UI" gap, if wired to a real route (confirm in PART D/J). |
| `templates/admin/reset_password_email.html` | `??` | A | New — closes the parity audit's **P0 crash bug** (missing email template causing every admin password-reset to 500). High-value fix, must ship. |
| `templates/admin/webhook_events.html` | `??` | A | New — closes the parity audit's P1 "no webhook visibility" gap, if wired (confirm PART D/J). |
| `templates/user/project_unavailable.html` | `??` | A | New — closes the parity audit's P1 "no styled unavailable page for suspended/missing public scanner projects" gap. |
| `tests/integration/test_user_projects_page.py` | `??` | A | New test — expected co-change with visual-polish batch. |
| `tests/integration/test_v1_agent2_admin_parity.py` | `??` | A | New test — name suggests it directly targets the parity-audit findings; strong signal this batch was built specifically to close that audit's gaps. |
| `SCANSTORY_V1_GAP_AUDIT.md` | `??` | C | Read-only reference document (per task instructions), sits at repo root. **Recommendation: do not commit into the application repo root** — if it must be preserved, relocate to `docs/handover/` or an audit-archive location outside the shipped tree in a future housekeeping pass; do not include in this release's staging manifest as-is. |
| `routes_map.txt` | `??` | B | Generated route dump (`flask routes`-style output) — a local debugging artifact, not source. Exclude. |
| `instance/` (7 `.db` files) | `??` | B | Local SQLite databases, several explicitly named `*_wave5_smoke_*`, `*_merge_check.db`, `*_wave7_device_test.db`, plus the live local dev DB `scanstory_local.db`. **Never commit** — these are per-developer local state and/or scratch test artifacts, not application code. |
| `"s -ExecutionPolicy RemoteSigned) ; (& f:ScanStory-mainScanStory-mainvenvScriptsActivate.ps1)"` | `??` | B | **Confirmed still present.** This is the previously-identified shell-mishap garbage filename from a botched PowerShell venv-activation command (the literal argument string got written to disk as a filename, complete with embedded backslashes/parentheses, when a quoting mistake caused PowerShell to treat part of a command line as a file redirection target). It is not source code and has zero product purpose. **Delete it from the working tree; never stage it.** |
| `windows_rq_worker.py` | `??` | B | **LOCAL WINDOWS QA ONLY per this task's explicit instruction.** Confirmed (see PART H) to be a trivial `SimpleWorker`/`TimerDeathPenalty` subclass working around native Windows' lack of `SIGALRM`. Not imported by any committed code. Must be excluded from staging/production; if kept in the dev workflow at all, it belongs in a clearly-marked local-tooling location, not committed to the shipped application tree as-is. |

## Q.4 Explicit exclusions confirmed

- `node_modules/`, `package-lock.json` were checked and are **not** present in `git status --short` output — confirms they are already gitignored (only `package.json` itself is tracked, unmodified). No action needed.
- No `.env` file appears in the dirty list — confirmed no secret file is at risk of being committed.
- `__pycache__/` directories (visible in `ls`) are not in `git status --short` — already gitignored.

## Q.5 Suggested explicit staging manifest (NOT executed — literal `git add` lines for human review only)

```
git add app.py
git add static/css/design-system.css
git add static/css/tailwind.build.css
git add static/js/nav.js
git add templates/admin/activity_logs.html
git add templates/admin/add_plan.html
git add templates/admin/base.html
git add templates/admin/capacity.html
git add templates/admin/edit_plan.html
git add templates/admin/forgot_password.html
git add templates/admin/login.html
git add templates/admin/manage_admins.html
git add templates/admin/payments.html
git add templates/admin/plans.html
git add templates/admin/project_preview.html
git add templates/admin/projects.html
git add templates/admin/reset_password.html
git add templates/admin/reset_password_email.html
git add templates/admin/scans.html
git add templates/admin/settings.html
git add templates/admin/subscriptions.html
git add templates/admin/user_profiles.html
git add templates/admin/user_scans.html
git add templates/admin/users.html
git add templates/admin/view_payment.html
git add templates/admin/view_project.html
git add templates/admin/view_user.html
git add templates/admin/webhook_events.html
git add templates/user/blog.html
git add templates/user/blog_articles/article.html
git add templates/user/contact.html
git add templates/user/dashboard.html
git add templates/user/edit_project.html
git add templates/user/experiences/detail.html
git add templates/user/experiences/list.html
git add templates/user/experiences/new.html
git add templates/user/experiences/public_unavailable.html
git add templates/user/experiences/public_viewer.html
git add templates/user/experiences/trigger_new.html
git add templates/user/forgot_password.html
git add templates/user/landing.html
git add templates/user/login.html
git add templates/user/payment_success.html
git add templates/user/privacy_policy.html
git add templates/user/profile.html
git add templates/user/project_preview.html
git add templates/user/project_unavailable.html
git add templates/user/projects.html
git add templates/user/register.html
git add templates/user/reset_password.html
git add templates/user/scanner.html
git add templates/user/subscribe.html
git add templates/user/success.html
git add templates/user/terms.html
git add templates/user/user_create_project.html
git add templates/user/verify_email.html
git add tests/integration/test_admin_navigation_routing.py
git add tests/integration/test_admin_panel_repair.py
git add tests/integration/test_admin_projects_module.py
git add tests/integration/test_super_admin_authorization.py
git add tests/integration/test_user_projects_page.py
git add tests/integration/test_v1_agent2_admin_parity.py
git add tests/security/test_csrf_and_headers.py
git add tests/security/test_otp_security.py
```

**Explicitly excluded from the manifest (Class B — never add):** `instance/` (all `.db` files), `routes_map.txt`, `windows_rq_worker.py`, the shell-mishap garbage filename, and (Class C, human decision needed first) `SCANSTORY_V1_GAP_AUDIT.md`'s repo-root location.

**No `git add .` / `git add -A` is suggested anywhere in this document, per instruction.**

---

---

# PART H (partial, direct verification) — CLI / Operations Command Register

Verified directly via `grep -n "@app.cli.command" app.py` (15 commands total, matching the prior parity audit's count):

| Command | Location | Purpose | Mutating? |
|---|---|---|---|
| `migrate-scanlog-session-uniqueness` | `app.py:852` | Adds a unique index for scan-log session dedup; `--apply` flag, default dry-run | Yes, gated by `--apply` |
| `migrate-otp-security-schema` | `app.py:1187` | Applies OTP-hardening schema changes; `--apply` flag, default dry-run/inspect | Yes, gated by `--apply` |
| `reconcile-quota-counters` | `app.py:2496` | Detects/repairs drift between stored `projects_used`/`scans_used` counters and live counts; `--repair` flag, default dry-run | Yes, gated by `--repair` |
| `capacity-status` | `app.py:2532` | Reports current capacity configuration/consumption | Read-only |
| `expire-stale-reservations` | `app.py:2544` | Expires stale `PaymentReservation` rows; `--apply` flag, default dry-run | Yes, gated by `--apply` |
| `reconcile-capacity-reservations` | `app.py:2566` | Repairs capacity counter vs. reservation drift; `--apply` flag, default dry-run | Yes, gated by `--apply` |
| `recover-processing-jobs` | `app.py:2607` | Recovers stuck `ProcessingJob` rows older than `--older-than-minutes` (default 30) | Yes |
| `seed-dev-test-users` | `app.py:2788` | Seeds dev/test user fixtures | Yes — dev/test only |
| `delete-dev-test-users` | `app.py:2796` | Deletes dev/test user fixtures; `--dry-run` flag | Yes, gated by `--dry-run` inverse |
| `rebuild-pair-features` | `app.py:3914` | Rebuilds OpenCV feature artifacts for a given `--project-id` | Yes |
| `cleanup-upload-sessions` | `app.py:6667` | Expires/deletes stale resumable-upload temp sessions/files; `--apply` flag, default dry-run | Yes, gated by `--apply` |
| `webhook-events-status` | `app.py:7391` | Lists recent Razorpay webhook events, `--limit` (default 20) | Read-only |
| `reconcile-order-webhooks` | `app.py:7411` | Shows webhook history for one `order_id` | Read-only |
| `webhook-replay-report` | `app.py:7432` | Aggregate replay/duplicate-delivery report | Read-only |
| `reconcile-payment-activations` | `app.py:7441` | Activates eligible pending orders; `--apply` flag, default dry-run | Yes, gated by `--apply` |

**Correction to prior lineage audit**: `SCANSTORY_V1_REPOSITORY_LINEAGE_AUDIT.md` §13 flagged `reconcile-payment-activations` as "referenced in a docstring but never implemented." **This is now stale** — the command is fully implemented at `app.py:7441` in current HEAD. STATUS: VERIFIED (current code supersedes the older document).

# PART P (partial, direct verification) — Health/Readiness Contract

**VERIFIED FACT**: `GET /healthz` (`app.py:592-596`) returns `{"status": "ok"}`, HTTP 200, `Cache-Control: no-store` — pure liveness, no dependency checks.
**VERIFIED FACT**: `GET /ready` (`app.py:613-629`, backed by `_readiness_checks()` at `app.py:599-608`) checks (a) DB via `SELECT 1`, and (b) queue mode — if `queue_mode() == "rq"`, it also calls `redis_ready_check()`; if Redis is unreachable or `QueueUnavailable` is raised, it reports `{"database": "ok", "queue": "unavailable"}` and the route returns **HTTP 503**. On any other exception it rolls back the session, logs a warning (`readiness_check_failed`), and does not leak exception text into the response body.
**OPERATIONAL IMPLICATION**: `/ready` is a genuine two-dependency readiness probe (DB + Redis/RQ) — a load balancer/orchestrator should gate traffic admission on this, not just `/healthz`.
**HANDOVER NOTE**: document `/healthz` as liveness-only and `/ready` as the real dependency gate for the Monitoring & Health Checks handover doc; matches `docs/production/monitoring-alerting.md` exactly.
**SOURCE**: `app.py:592-629`; corroborated by `docs/production/monitoring-alerting.md` "Health Contracts" section (already-existing production doc, read in full during this audit).
STATUS: VERIFIED.

---

# PART L — Server-Team Deployment Evidence (seeded from `docs/production/README.md`, cross-checked)

This repository already contains a dedicated, current, high-quality operations doc set at `docs/production/` (`README.md`, `deployment-runbook.md`, `database-migration-runbook.md`, `rollback-runbook.md`, `backup-restore-runbook.md`, `monitoring-alerting.md`, `razorpay-certification.md`, `security-proxy-checklist.md`, `staging-certification.md` — all read in full during this audit). Rather than re-deriving this from scratch, this Part cites it directly as VERIFIED source material and flags anything it does not cover as `SERVER_TEAM DECISION REQUIRED`.

| Requirement | Verified value / requirement | Source | Status |
|---|---|---|---|
| OS for RQ worker | Linux required for the standard `rq_worker.py`/`rq worker` flow (SIGALRM-based timeouts) | PART H (below), `windows_rq_worker.py` analysis | VERIFIED |
| Python version | Not pinned in repo (no `.python-version`/`runtime.txt` found in this pass) | — | `SERVER_TEAM DECISION REQUIRED` |
| Dependencies | `requirements.txt` (production) — see PART R for full audit; ceiling-only `<=` pins | `requirements.txt` | VERIFIED (pin style), TBD (exact install version) |
| Database | PostgreSQL required in production; `DATABASE_URL` — "Startup rejects SQLite or non-PostgreSQL URLs in production" | `docs/production/README.md` env table | VERIFIED |
| Redis | Required in production when `SCANSTORY_QUEUE_MODE=rq`; `REDIS_URL` — "production startup rejects missing Redis" | `docs/production/README.md` | VERIFIED |
| Queue | `SCANSTORY_QUEUE_MODE` must be `rq` in production; `fake`/`inline` are dev/test only; `RQ_QUEUE_NAME` default `scanstory-processing` | `docs/production/README.md` | VERIFIED |
| WSGI/Gunicorn | `gunicorn<=21.2.0` present in `requirements.txt`; exact worker count/timeout/binding = `SERVER_TEAM DECISION REQUIRED` | `requirements.txt` | VERIFIED (dependency present), TBD (config) |
| Reverse proxy | Required; must overwrite forwarded headers, not append; `ProxyFix(x_for=1, x_proto=1, x_host=1)` must match **exactly one** trusted proxy hop | `docs/production/security-proxy-checklist.md` | VERIFIED (app-side contract), `SERVER_TEAM DECISION REQUIRED` (actual proxy product/config) |
| HTTPS/TLS | TLS terminates at the trusted proxy/upstream; `X-Forwarded-Proto` must accurately represent HTTPS; `SESSION_COOKIE_SECURE` must be `1` behind HTTPS | `docs/production/security-proxy-checklist.md`, `README.md` env table | VERIFIED (requirement), `SERVER_TEAM DECISION REQUIRED` (cert provisioning) |
| Domain/DNS | Not defined anywhere in repo | — | `SERVER_TEAM DECISION REQUIRED` |
| Static files | Served by Flask by default; no CDN/object-storage config found in this repo | — | `SERVER_TEAM DECISION REQUIRED` if CDN desired |
| Media storage | `SCANSTORY_DATA_DIR` (user media), `SCANSTORY_ADMIN_DATA_DIR` (admin media), `SCANSTORY_STATIC_UPLOADS_DIR` (static uploads) — all "required, production-only, no safe default" | `docs/production/README.md` | VERIFIED |
| File permissions | Writable storage paths must exist and be owned by the application user (pre-deployment checklist item) | `docs/production/README.md` | VERIFIED (requirement stated), `SERVER_TEAM DECISION REQUIRED` (exact ownership/uid) |
| Upload limits | Resumable chunk body cap `SCANSTORY_RESUMABLE_CHUNK_MAX_BYTES`, default 1 MiB; reverse-proxy body-size limit for `/api/uploads/sessions/*/chunk` must allow at least this and "should not greatly exceed it" | `docs/production/README.md` "Current Integrated Runtime Constants" | VERIFIED |
| Proxy body limit | Must match the resumable chunk cap above at the proxy layer | Same | `SERVER_TEAM DECISION REQUIRED` (exact proxy directive) |
| SMTP | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` required for email, no safe default; `MAIL_FROM` optional (defaults to `SMTP_USER`) | `docs/production/README.md` | VERIFIED |
| Razorpay webhook | `RAZORPAY_WEBHOOK_SECRET` required for webhook reconciliation, secret, no safe default, distinct from `RAZORPAY_KEY_SECRET`; route fails closed if absent | `docs/production/README.md`, `razorpay-certification.md` | VERIFIED |
| reCAPTCHA domain | Presence/exact env vars pending PART K agent confirmation | — | TBD (filled below once PART K lands) |
| Secure cookies | `SESSION_COOKIE_SECURE` required in production (safe default only for local); `SECURITY_HSTS_ENABLED` optional, production-only | `docs/production/README.md` | VERIFIED |
| Migrations | `flask db heads` / `flask db history` / `flask db current` must be run in that order before any upgrade; offline SQL review via `flask db upgrade --sql` where supported | `docs/production/database-migration-runbook.md` | VERIFIED |
| Bootstrap admin | `BOOTSTRAP_ADMIN_ENABLED` (safe default off), `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD` — "Disable after initial setup" / "Rotate/remove after bootstrap" | `docs/production/README.md` | VERIFIED |
| Backups | Database + media backups at least daily plus pre-deployment; retention = business policy (not defined in repo) | `docs/production/backup-restore-runbook.md` | VERIFIED (scope/frequency floor), TBD (retention — see PART O) |
| Restores | Full restore rehearsal procedure exists and must be run at least once before production launch | `docs/production/backup-restore-runbook.md` | VERIFIED (procedure exists), `SERVER_TEAM_VERIFY` (has it actually been rehearsed against real infra yet — not confirmable from this repo alone) |
| Logging | Logs must never contain: passwords, Razorpay key secret, webhook secret, raw payment signatures, raw `X-Razorpay-Signature`, raw webhook body, auth cookies, private media paths, full request bodies with files, customer email in payment payload logs | `docs/production/monitoring-alerting.md` "Log Hygiene" | VERIFIED (contract), pending PART F confirmation this is enforced in code, not just documented |
| Process supervision | Not defined in repo (no systemd unit/Procfile/supervisor config found in this pass) | — | `SERVER_TEAM DECISION REQUIRED` |
| Health/readiness | `/healthz` liveness, `/ready` DB+queue readiness — see PART P above | `app.py:592-629` | VERIFIED |
| Worker health | No dedicated RQ-worker health endpoint found; `/ready`'s queue check reflects Redis reachability only, not worker-process liveness | `app.py:599-608` | VERIFIED (gap), `SERVER_TEAM DECISION REQUIRED` (worker process monitoring — see PART P full split) |
| Rollback | Application-only, migration/data, and media/storage rollback procedures all documented | `docs/production/rollback-runbook.md` | VERIFIED |

# PART M — Deployment Runbook Seed (cited from `docs/production/deployment-runbook.md`, already exists and is current)

The repository already contains a fully-sequenced 30-step deployment runbook. Rather than re-derive a new sequence, this audit adopts it as VERIFIED and notes that it already matches the exact sequence requested by this task's brief (obtain release → verify → env setup → dependencies → provision PostgreSQL/Redis → configure env → media dirs → migrations → validate config → start app/worker → reverse proxy/TLS → SMTP check → Razorpay test webhook → health/readiness → smoke test → scanner HTTPS/device test → backup validation → sign-off):

1. Freeze release commit, record full SHA. 2. Confirm `git status --short` clean. 3–5. Capture + verify DB and media backups. 6. Verify env vars **without printing values**. 7–9. Place release files, install deps from approved requirements file, record artifact/version. 10. Verify writable paths (images/videos/features/QR/logs). 11–13. `flask db heads` / `history` / `current`. 14. Migration duplicate-preflight. 15–16. Offline SQL review, then migrate only after explicit approval. 17. Restart app. 18–19. Verify `/healthz` and `/ready`. 20–27. Smoke tests: login, admin/super-admin, project upload, scanner load + API contract, public media Range response, suspended-project blocking, Razorpay test-mode order+activation in staging. 28. Verify logs contain no secrets/PII per the Log Hygiene list (PART L). 29. Release traffic. 30. Monitor health/readiness/error-rate/payment-activation/scanner-latency/storage.

**Deployment stop conditions** (verified, same doc): migration preflight failure, unverifiable backups, `/healthz`/`/ready` failure after restart, login/upload/scanner/payment smoke failure, authorization regression, secret/PII leakage in logs, error rate over threshold — escalate to `[Rollback Authority Role]` (a named-role placeholder the repo deliberately leaves for the server/business team to fill in — this is a genuine `SERVER_TEAM DECISION REQUIRED`, not an oversight).

STATUS: VERIFIED (the runbook exists and is current); `SERVER_TEAM DECISION REQUIRED` for the `[Rollback Authority Role]` placeholder specifically, and for translating steps 7–9/17 into this org's actual deploy tooling (no CI/CD pipeline config was found in this repo).

# PART N — Operations / Incident / Recovery Audit (cited from `docs/production/incident-response.md`, already exists and is current)

The repo's `incident-response.md` already defines first-response procedures for exactly the categories this task requires, each with detection/effect/first-check/recovery-tool/escalation structure. Verified categories present: **Razorpay Payment Captured but Activation Missing**, **Razorpay Webhook Rejected/Not Processing**, **Duplicate Callback/Verification/Webhook Delivery**, **Capacity Counter or Reservation Drift**, **Expired Reservations**, **Database Unavailable**, **Disk Full**, **Media Missing**, **Scanner Latency Spike**, **Brute-Force/Login-Rate Alert**, **Suspected Forwarded-Header Spoofing**, **Suspected Secret Exposure**, **Suspended Media Still Visible from Browser Cache**.

Mapped to this task's requested incident categories:

| Category | Detection | Recovery tool (verified) | Escalation |
|---|---|---|---|
| APPLICATION DOWN | `/healthz` failure | Restart process; app-only rollback runbook | `[Rollback Authority Role]` (TBD who) |
| DATABASE DOWN | `/ready` returns 503 with `database` check failed | Check connectivity/credentials via secret manager; failover/restore per DB ops policy (no in-repo DB failover automation found) | Server/DBA team |
| REDIS DOWN | `/ready` returns 503 with `queue: unavailable` | Check Redis reachability; queue mode config | Server/infra team |
| WORKER DOWN | No dedicated worker-health signal in-repo (see PART P gap) — inferred from stuck `ProcessingJob` rows / `recover-processing-jobs` CLI | `flask recover-processing-jobs --older-than-minutes N` (`app.py:2607`) | App maintenance |
| PAYMENT WEBHOOK FAILURE | Log line `razorpay_webhook_rejected reason=secret_not_configured` / `missing_signature` / `invalid_signature` | `flask webhook-events-status`, `flask reconcile-order-webhooks <order_id>`, `flask webhook-replay-report` (all read-only) | Payments/finance + app maintenance |
| SMTP FAILURE | Not explicitly covered by a named incident category in `incident-response.md` — a documented P3 gap per the lineage audit ("no dedicated SMTP test file found") | No dedicated CLI recovery tool found | `SERVER_TEAM DECISION REQUIRED` / app maintenance |
| MEDIA/STORAGE FAILURE | "Disk Full" / "Media Missing" categories | Stop uploads, expand storage, restore from media backup | Server/infra team |
| SCANNER ISSUE | "Scanner Latency Spike" category; explicitly instructs "confirm no recent scanner algorithm change entered the release" | Vertical scale / traffic reduction per ops policy; no auto-remediation | App maintenance + server team |
| CAPACITY ISSUE | "Capacity Counter or Reservation Drift" / "Expired Reservations" | `flask capacity-status`, `flask expire-stale-reservations [--apply]`, `flask reconcile-capacity-reservations [--apply]` | App maintenance |
| AUTH ISSUE | "Brute-Force/Login-Rate Alert" / "Suspected Forwarded-Header Spoofing" | Block at edge, review login/OTP logs, confirm ProxyFix trust | Security + server team |

STATUS: VERIFIED for all categories with an existing doc entry; **SMTP FAILURE is a genuine, honestly-disclosed gap** — no dedicated incident procedure or CLI recovery tool was found for SMTP outages beyond generic log review. Flagged `SERVER_TEAM DECISION REQUIRED` / candidate P3 in the risk register (PART X).

# PART O — Backup/Restore Audit (cited from `docs/production/backup-restore-runbook.md`, already exists and is current)

**What must be backed up** (verified, matches this task's requirement list exactly): relational database, uploaded marker images, uploaded videos, generated feature artifacts, QR assets, configuration secrets (through the approved secret manager — i.e., NOT via this repo's backup process), Alembic migration version information.

**Frequency**: "at least daily plus pre-deployment backup" for both DB and media/artifacts — this is the one frequency commitment the repo does make.

**Retention: `BACKUP RETENTION = TBD BY BUSINESS/SERVER TEAM`** — verified the repo does NOT specify a retention schedule; `backup-restore-runbook.md` only says "keep short-term daily backups and longer-term weekly/monthly backups according to business policy," which is explicitly deferred to business decision, not a concrete number. This audit will not invent one.

**Integrity verification** (verified procedure exists): exit-code check, non-empty file check, checksum recorded, sample DB restore into isolated environment, sample media restore + decode representative files, feature-artifact/QR-asset presence check.

**Restore rehearsal** (verified procedure exists, at least once before launch): restore DB → restore media → start app against restored copy → verify Alembic current version → health/readiness → functional smoke (login/admin/upload/scanner/media/suspension/payment test-mode activation).

`SERVER_TEAM DECISION REQUIRED`: actual backup tooling/vendor, retention duration, off-host encrypted copy location/provider, and whether the restore rehearsal has actually been executed against real infrastructure (this repo can only confirm the *procedure* exists, not that it has been run — flag as `SERVER_TEAM_VERIFY`).

# PART P (continued) — Monitoring/Health Audit

**What the application ITSELF has (verified in-repo):**
- `/healthz` liveness (`app.py:592-596`), `/ready` DB+queue readiness (`app.py:599-629`).
- Structured webhook audit trail via `RazorpayWebhookEvent` + 3 read-only inspection CLIs.
- Admin activity log (`AdminActivity` model, `/admin/activity-logs` route).
- Scanner-side diagnostics/telemetry fields in `/detect_init` responses (per `SCANSTORY_V1_GAP_AUDIT.md` PF7, latency logging noted historically as `print()`-only — current state pending PART I agent confirmation).
- Processing-job status via `ProcessingJob` lifecycle fields (pending PART H agent confirmation of exact fields).

**What the SERVER PLATFORM must provide externally (NOT implemented in this repo — do not claim otherwise):**
- Process supervision/auto-restart (systemd/supervisor/container orchestrator).
- CPU/RAM/disk monitoring and alerting.
- TLS certificate expiry monitoring.
- Uptime/external synthetic monitoring hitting `/healthz` over the public HTTPS endpoint.
- Database backup success/failure monitoring.
- RQ worker process liveness monitoring (the app's own `/ready` only proves Redis is reachable, not that a worker is consuming the queue).
- Disk/storage growth alerting for the media directories.
- Centralized log aggregation and rotation (`docs/production/README.md` explicitly lists `LOG_LEVEL`/`STRUCTURED_LOGGING_ENABLED` as "future, not yet active — do not rely on this until logging config reads it").

Recommended alert thresholds are already drafted in `docs/production/monitoring-alerting.md` (e.g. `/healthz` alert after 3 consecutive failures, `/ready` after 2, scanner p95 warning at 1s/critical at 3s, disk warning at 75%/critical at 90%, immediate alert on any payment-activation failure or secret-looking log event, immediate alert on `razorpay_webhook_rejected reason=secret_not_configured`) — these are documented *targets/suggestions* to tune with real traffic, not yet-implemented automated alerts, since no monitoring/alerting platform integration exists in this repo.

STATUS: VERIFIED for both "what exists" and "what's external"; the distinction is honestly maintained in the source doc and re-confirmed by this audit's own direct reads of `app.py`.

---

---

# PART G — Payment / Commercial Operations Audit

**Verified lifecycle**: plan → `/create-razorpay-order` (capacity slot reserved atomically **before** the Razorpay order is created) → Razorpay checkout → `/verify-payment` (browser) **or** `/webhooks/razorpay` (server-to-server) → both converge on one shared `activate_payment()` service → entitlement/quota update → CLI reconciliation as a safety net.

| # | VERIFIED FACT | EVIDENCE/SOURCE | OPERATIONAL IMPLICATION | STATUS |
|---|---|---|---|---|
| G1 | `/verify-payment` (and the webhook) are now idempotent. `activate_payment()` gates activation on a single conditional `UPDATE payment_orders SET status='success' WHERE id=? AND status='pending'`; if 0 rows update, it returns `{"success": True, "replay": True}` without touching quotas or `subscription_end`. | `app.py:6812` (`activate_payment`), `6872-6891` (atomic gate + replay branch), route `app.py:7056-7131`. Tests: `tests/integration/test_payment_idempotency_and_capacity.py:165,535`. | A replayed browser callback, double-click, or back-button resubmit can no longer reset a user's usage counters or re-extend their subscription for free — the prior audit's #1 money-path Critical is closed. | VERIFIED — supersedes `SCANSTORY_V1_GAP_AUDIT.md` C1 (STILL OPEN → now CLOSED) |
| G2 | A Razorpay webhook now exists: `POST /webhooks/razorpay`, HMAC-SHA256 verified via the Razorpay SDK's `Utility().verify_webhook_signature()` (confirmed to internally use `hmac.compare_digest`, constant-time), fails closed if `RAZORPAY_WEBHOOK_SECRET` is unset or the signature is missing/invalid, and reuses `activate_payment()` verbatim rather than reimplementing activation logic. | `app.py:7223-7388` (route), `7149-7167` (signature check), `7364` (calls `activate_payment`). Model: `RazorpayWebhookEvent` (`models.py:439-478`) with a DB **unique index** on `idempotency_key` as the replay gate (not an in-memory check). Tests: `tests/integration/test_razorpay_webhook_reconciliation.py` (25 tests). | A payment captured by Razorpay is now activated even if the browser never returns (tab closed, network drop) — the prior audit's #2 (no webhook, C3) money-path Critical is closed. Only `payment.captured` mutates anything; every other validly-signed event type is acknowledged (`200`) with zero mutation — an explicit, documented scope boundary, not a silent gap (`docs/production/razorpay-certification.md`). | VERIFIED — supersedes GAP_AUDIT C3 |
| G3 | A hard paid-account capacity gate now exists: `CapacityConfig` (singleton row, `configured_limit` default 25 via `SCANSTORY_INITIAL_CAPACITY_LIMIT`) + `PaymentReservation` (lifecycle `reserved → activated / released / expired`, TTL via `SCANSTORY_CAPACITY_RESERVATION_TTL_MINUTES`, default 30). Reservation claim is a single conditional `UPDATE capacity_config SET consumed_count=consumed_count+1 WHERE id=1 AND enabled=1 AND consumed_count<configured_limit` — no prior SELECT/COUNT, race-safe. Reserved **before** the Razorpay order is even created; returns HTTP 503 `CAPACITY_FULL` if full. | `models.py:383-406` (`CapacityConfig`), `408-436` (`PaymentReservation`); `app.py:2083-2124` (`_reserve_capacity_slot_atomic`), `6953-6962` (called at order-creation). CLIs: `capacity-status` (`app.py:2532`), `expire-stale-reservations` (`2544`), `reconcile-capacity-reservations` (`2566`). Tests: `test_payment_idempotency_and_capacity.py:199,564,626`. | Nothing today allows a 26th/27th/Nth paid signup past the configured cap under concurrency — the prior audit's #3 (no capacity check, C2) money-path Critical is closed. Lowering the configured limit below the current active count does not evict existing users (verified by test). | VERIFIED — supersedes GAP_AUDIT C2 |
| G4 | A unique DB constraint on both `razorpay_order_id` and `razorpay_payment_id` now exists (`unique=True, nullable=True, index=True`), added via a dedicated migration with a pre-flight duplicate check that aborts (raises `RuntimeError`) rather than silently merging/deleting existing rows on conflict. Both activation paths catch `IntegrityError` on write and fail safely (409/failed-event) instead of crashing. | `models.py:343-344`; `migrations/versions/bc5642a86981_razorpay_id_unique_constraints.py`; `app.py:7106-7112, 7348-7354` (`IntegrityError` handling). Tests: `test_payment_idempotency_and_capacity.py:475,496,517`. | The database itself now prevents the same Razorpay identifier from ever landing on two different `PaymentOrder` rows — closes GAP_AUDIT C4. | VERIFIED |
| G5 | **No refund feature exists — confirmed, and now honestly disclosed rather than dead-linked.** The admin "Process Refund" / "Resend Receipt" buttons are rendered `disabled` with explicit tooltips stating the capability "is not available in this admin package." No refund route, no Razorpay refund API call, no `status="refunded"` write path anywhere in `app.py`. | `templates/admin/view_payment.html:723-729`; `app.py:7144` docstring explicitly scopes refund/chargeback/settlement events as "out of scope entirely." `docs/production/README.md`/`razorpay-certification.md` both state the same. | Support cannot process a refund through the app at all — must be handled directly in the Razorpay dashboard or via a manual, out-of-band process. This is a genuine, permanent V1 scope boundary, not a bug. | VERIFIED — do not build or imply a refund feature in the handover pack |
| G6 | Admin now has real UI, not just CLI, for two of the previously CLI-only areas: `/admin/capacity` (view/edit `configured_limit`/`enabled`, gated `@require_admin_permission("superadmin.capacity.manage")`, audit-logged) and `/admin/webhook-events` (read-only list/filter by order id, gated `@require_admin_permission("admin.payments.view")`, deliberately never renders raw payload/signature/secret). `reconcile-payment-activations` (activating eligible pending orders after manual investigation) remains **CLI-only**, intentionally — it's a rare, deliberate operator-recovery action, not a routine UI action. | `app.py:10643-10674` (`/admin/capacity`), `10680-10706` (`/admin/webhook-events`); `app.py:7441` (`reconcile-payment-activations` CLI, `--apply` flag, default dry-run). | Support/ops can now see and manage capacity and inspect webhook delivery history without shell access — closes the prior parity audit's P1 "no admin visibility" findings for capacity and webhook events. | VERIFIED — supersedes `SCANSTORY_V1_FEATURE_PARITY_AUDIT.md` C2/C3 |
| G7 | Full payment/capacity CLI register (8 commands, all `flask <name>`, mutating ones default to dry-run and require an explicit flag to persist): `reconcile-quota-counters [--repair]`, `capacity-status` (read-only), `expire-stale-reservations [--apply]`, `reconcile-capacity-reservations [--apply]`, `webhook-events-status [--limit N]` (read-only), `reconcile-order-webhooks <order_id>` (read-only), `webhook-replay-report` (read-only), `reconcile-payment-activations [--apply]`. | `app.py:2496,2532,2544,2566,7391,7411,7432,7441`. | Every reconciliation action an operator might need during an incident (PART N) has a named, safe-by-default (dry-run) CLI command. | VERIFIED |
| G8 | Webhook-supplied amount/currency are never trusted — the route independently compares against the stored `PaymentOrder` and rejects (`amount_mismatch`/`currency_mismatch`) on disagreement, same principle `/verify-payment` already applies to browser-supplied values. Unknown external order ids finalize as `unknown_order` with zero mutation. | `docs/production/razorpay-certification.md` (re-confirmed structurally by agent code read); `app.py:7223-7388`. | Razorpay-side or attacker-supplied payloads cannot escalate entitlement beyond what the server itself already computed and stored. | VERIFIED |

**HANDOVER NOTE (Payment & Subscription Operations doc)**: document the full lifecycle above, the CLI register verbatim, the explicit "no refund" scope boundary, and the fact that live Razorpay webhook delivery to a real HTTPS staging endpoint is still `SERVER_TEAM_VERIFY` (only mocked/simulated delivery has been exercised so far — see `docs/production/razorpay-certification.md` "Not Yet Certified" and PART X).

---

# PART F — Security Audit

| # | VERIFIED FACT | EVIDENCE/SOURCE | STATUS |
|---|---|---|---|
| F1 | CSRF enabled globally (`WTF_CSRF_ENABLED=True`, `WTF_CSRF_CHECK_DEFAULT=True`), `CSRFProtect(app)` instantiated. Exactly 6 `@csrf.exempt` routes, each with an inline justification: `/webhooks/razorpay` (HMAC-verified, no cookie session to forge) and 5 public/unauthenticated scanner endpoints (no session to bind a token to). | `app.py:94-96,223`; exemptions at `app.py:7224,7820,8447,8586,8709,8776`. Tests: `tests/security/test_csrf_and_headers.py:104,223,235`. | VERIFIED |
| F2 | Session cookies: `HTTPONLY=True` and `SAMESITE="Lax"` unconditionally; `SECURE` defaults False for local HTTP dev but is a hard boot-time-enforced `True` requirement in production mode. | `app.py:161-162,188,218-220`. | VERIFIED |
| F3 | Security headers always sent: `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, `Permissions-Policy`. CSP is env-gated: `SECURITY_CSP_ENABLED` (default True) + `SECURITY_CSP_ENFORCE` (default False) → report-only by default, enforcing only when explicitly flipped. HSTS gated by `SECURITY_HSTS_ENABLED` (default False) and only sent over genuine HTTPS. | `app.py:515,526-527,533-586`. Tests: `test_csrf_and_headers.py:269,330`. | VERIFIED |
| F4 | OTP codes are hashed (`generate_password_hash`), generated via CSPRNG (`secrets.randbelow`), attempt-locked, and IP-throttled independently on both verify and resend (env-tunable limits, e.g. `SCANSTORY_OTP_IP_ATTEMPT_LIMIT` default 30, `SCANSTORY_OTP_IP_RESEND_LIMIT` default 10). OTP values never appear in logs (tested). | `app.py:1104-1105,1127-1128,1217-1245,1266-1272,1317-1327,1390`. Tests: `tests/security/test_otp_security.py` (39 tests, e.g. `:219,227,556,569`). | VERIFIED |
| F5 | Rate limiting exists on upload (8/hour), user-login-by-IP (80/900s), scanner `detect_init` (45/60s), and scanner `detect_track` (240/60s) — via an in-process `InMemoryRateLimiter`. Its own module docstring explicitly discloses it is **process-local only**, not shared across Gunicorn workers, and must be replaced with a Redis-backed limiter before horizontal scaling. **Admin login has no IP-based rate limit** — it relies solely on the per-account DB-persisted lockout (F6), not on request-rate throttling. | `rate_limit.py:1-7,13-61`; `app.py:284-296,305-307,5063,5320,7834,8786`. | VERIFIED (present + honestly self-documented ceiling); admin-login IP throttle absence flagged in PART X |
| F6 | Admin login lockout and audit logging now exist — DB-persisted (via `system_config`, survives restart, unlike an in-memory counter), locks after a configured attempt threshold, HTTP 429 on lockout, and every admin login attempt (success, failure, lockout) is written to the `AdminActivity` audit log. | `app.py:1536-1560,8955-9001,1702`. | VERIFIED — supersedes GAP_AUDIT SE4 (was "missing", now fixed) |
| F7 | Upload content validation is real magic-byte + full decode/probe validation, not filename/Content-Type/extension trust. Images: magic-byte check + forced `Image.verify()` + full strict re-decode (rejects animated/multi-frame). Videos: ISO-BMFF `ftyp` box check + `cv2.VideoCapture` frame-read confirmation (rejects container-valid-but-undecodable files). | `upload_validation.py:1-11,33-41,68-150,153-210`. Tests: `tests/security/test_upload_validation.py:156,162,173,200`. | VERIFIED |
| F8 | No path-traversal risk — every served media filename is server-generated from integer project/pair IDs (e.g. `f"{project_id}_{i}.jpg"`), never derived from client-supplied names; serving uses `send_from_directory()` against server-known filenames only. | `upload_validation.py:53-65`; `app.py:3950,3983,5446,6378,10860,7588,7604,7612,11051,11066,11085`. Test: `tests/security/test_upload_validation.py:244`. | VERIFIED |
| F9 | Razorpay webhook HMAC verification uses the SDK's own `verify_webhook_signature()`, confirmed (by reading the installed SDK source) to use `hmac.compare_digest` internally — genuinely constant-time, not a hand-rolled string comparison. | `app.py:7149-7167`; SDK source `razorpay/utility/utility.py:56-73`. | VERIFIED |
| F10 | Payment replay protection is two independent DB-level gates, not in-memory: webhook replay is rejected by a DB unique-index `IntegrityError` on `idempotency_key`; browser-path replay is rejected by the `activate_payment()` conditional `UPDATE ... WHERE status='pending'` updating 0 rows. Both paths funnel through the same service, so a browser/webhook race activates exactly once regardless of arrival order. | `app.py:7174-7205,6872-6891`. Tests: `test_razorpay_webhook_reconciliation.py:386,410,433`. | VERIFIED |
| F11 | Quota/capacity concurrency uses one shared atomic-conditional-UPDATE primitive (`_atomic_increment_user_counter`) for project and scan counters, row-level locking (`with_for_update()`, Postgres/MySQL-only, degrades to whole-DB lock on SQLite) for pair-slot quota, and the same atomic-UPDATE pattern for capacity slots. | `app.py:1986-1987,1990-2007,2010-2038,2041-2045`, plus G3 above. | VERIFIED |
| F12 | Admin privilege model upgraded from a single plain `role != "superadmin"` string check to fine-grained `@require_admin_permission(...)` decorators on the highest-sensitivity routes — `/admin/plans*` → `superadmin.plans.manage`; `/admin/settings` → `superadmin.settings.manage`; `/admin/capacity` → `superadmin.capacity.manage`; `/admin/webhook-events` → `admin.payments.view`. `super_admin_required` itself is now a thin wrapper around this same permission system. Plain `@admin_required` remains correctly used at lower-sensitivity routes. | `app.py:9533,9561,9685,9811,9841,10603,10644,10681,1938-1939,1929-1936`. Test: `tests/integration/test_super_admin_authorization.py:17,58`. | VERIFIED — supersedes GAP_AUDIT A4 |
| F13 | **One open finding**: a plaintext user email is printed via `print()` on every scan-attribution call (`app.py:8510`). By contrast, OTP codes, webhook payloads/secrets, and raw signatures are all confirmed (by dedicated tests) never to appear in logs. General request logging (`log_incoming_request`/`log_outgoing_response`) prints client IP unstructured to stdout on every non-static request. | `app.py:8510,495-509`. Tests proving the *absence* of leakage elsewhere: `test_otp_security.py:569`, `test_razorpay_webhook_reconciliation.py:232,602`. | **STILL OPEN** (minor, P3 — see PART X) |
| F14 | Production boot-time config validation fails startup (raises `RuntimeError` listing every missing var) if, in production mode: `FLASK_SECRET_KEY`, a genuine PostgreSQL `DATABASE_URL`, full SMTP config, `SCANSTORY_QUEUE_MODE=rq`, `REDIS_URL`, or `SESSION_COOKIE_SECURE=true` are missing, or if `SCANSTORY_DEV_TESTING=1` is set. This runs eagerly at import time, before `app.secret_key` is even assigned. | `app.py:135-170,173`. | VERIFIED — supersedes GAP_AUDIT's earlier "no boot-time validation" style findings |
| F15 | Dependency pins remain ceiling-only (`<=X`, no lower bound) across all 28 entries in `requirements.txt` (e.g. `Flask<=2.3.3`, `cryptography<=42.0.7`, `redis<=5.0.8`). No exact `==` pins or lockfile exist. | `requirements.txt` (full file). | **STILL OPEN** — unchanged from GAP_AUDIT SE6; see PART R and PART X |

**HANDOVER NOTE (Security & Access Control Guide)**: F1–F12 and F14 close essentially every P0/P1 security finding from the three prior audits. F13 (one `print()` of a plaintext email) and F15 (ceiling-only pins) are the only two genuinely still-open items, both low severity — carry them into PART X as P3.

---

---

# PART E — Data Model / Database Audit

Source: `models.py` (1573 lines), `processing_jobs.py` (161 lines), verified directly.

| Model | Lines | Purpose | Key fields | Relationships | Ownership | Lifecycle | Operational importance |
|---|---|---|---|---|---|---|---|
| `SubscriptionPlan` | 23–117 | Sellable plan/pricing tier | `plan_amount`, `offer_price`, `duration_type/value`, `trial_days`, `total_project_limit`, `total_scan_limit`, `is_trial_plan` | `created_by`→admins | Admin-owned | Created by admin, referenced at purchase | Money — defines entitlement |
| `User` | 123–280 | Customer account + usage state | `email`(unique), `password_hash`, `is_verified`, `is_blocked`, `subscription_status`, `projects_used`, `scans_used` | `subscription_id`→plans; cascades to trial/otp/projects/payments/logins | Self | trial→active→expired/limit_reached via `refresh_limit_status()` | Security + money — `can_create_project`/`can_scan` (217-229) are the real enforcement surface |
| `TrialDetails` | 286–327 | Per-user free-trial window | `trial_start/end`, `trial_project_limit/scan_limit`, `trial_extended`, `trial_converted` | 1:1 with User; `extended_by`→admins | User + admin-extendable | `is_active` property drives `has_active_subscription()` | Support audit trail for trial extensions |
| `PaymentOrder` | 333–377 | One Razorpay order/checkout | `razorpay_order_id`/`razorpay_payment_id` (now **unique**), `status` (pending/success/failed/refunded), `subscription_start/end` | `user_id`, `plan_id`; back-ref from `PaymentReservation`, `RazorpayWebhookEvent` | User-owned | pending→success/failed/refunded | Core billing ledger row |
| `PaymentReservation` | 408–436 | Atomic capacity-slot holder | `status` (reserved/activated/released/expired), `expires_at` | `user_id`, `payment_order_id` | User-owned | reserved→activated (permanent) \| released \| expired | Capacity invariant: `CapacityConfig.consumed_count == count(reserved+activated)` |
| `CapacityConfig` | 383–405 | Singleton (id=1) capacity gate | `configured_limit` (default 25), `enabled`, `consumed_count` | none | Admin-configured | Mutated only via atomic conditional UPDATE | Launch gate on paid signups; lowering the limit never evicts active users (tested) |
| `RazorpayWebhookEvent` | 439–481 | Webhook delivery idempotency/audit row | `idempotency_key` (**unique** — the real replay gate), `event_type`, `payload_hash` (raw body never persisted), `processing_status`, `failure_code` | `payment_order_id` | System | received→processed/ignored/failed | DB-level replay protection for `/webhooks/razorpay` |
| `ProcessingJob` | 1326–1389 | Generic async job/state-machine row | `status` (large explicit state set), `queue_job_id`, `attempt_count/max_attempts`, `idempotency_key`, `claimed_by`/`lease_expires_at`, `safe_error_code/summary` vs `internal_diagnostics` | Optional FKs to legacy Project/Pair AND new Workspace/Experience/Trigger simultaneously | Mixed | pending→ready→claimed→running→succeeded/failed_* | Now wired into the live RQ pipeline (migration `a73f2c19d8e2` added the project/pair/owner columns) — see PART H |
| `UploadSession` | 1456–1553 | One resumable chunked-upload attempt | `owner_user_id`/`owner_admin_id`, `current_offset`, `status` (active/finalizing/assembled/completed/cancelled/expired/failed), `storage_token` (server-generated UUID4, never client-derived) | `project_id`, `pair_id` (populated on completion) | User or admin | active→finalizing→assembled→completed \| failed \| cancelled \| expired | `assembled` means quota already consumed but enqueue failed — retryable without double-charging quota |
| `ScanLog` | 816–835 | One scan attempt, 1-per-session enforced | `scan_session_id`, `is_successful`, `counted` | `project_id`, `pair_id`, `user_id`; `UniqueConstraint(user_id, scan_session_id)` | System-recorded | Append-only | `is_successful=True` is what consumes scan quota |
| `ScanEvent` | 855–905 | Fallback/analytics event log — deliberately separate from `ScanLog` | `event_type` ∈ {pair_fallback_view, project_fallback_view, recognition_timeout, camera_unavailable}, `client_event_id` (unique idempotency key) | `project_id`, `pair_id` | System-recorded | Multiple rows per session allowed | Explicitly documented to NOT contaminate `ScanLog`'s success/count aggregates — the F1/F2 fallback-analytics gap from `SCANSTORY_V1_GAP_AUDIT.md` §13 is now closed |
| `OTPCode` | 487–525 | Auth-state row for email OTP | `code_hash`, `purpose`, `challenge_id` (unique), `locked_until`, `attempt_count/max_attempts`, `resend_count`, `ip_address` | `user_id` (nullable) | Tied to email/user | Expires via `is_expired` property | Primary anti-abuse surface for auth |
| `Admin` | 531–581 | Staff/operator account | `role`, `permissions_json`, `is_active` | `created_by`→admins (self-referential) | Hierarchical | No soft-delete beyond `is_active` | Full-privilege internal actor |
| `Project` | 587–637 | Container for AR image/video pairs | `owner_user_id` XOR `owner_admin_id`, `qr_code_path`, `is_active`, `fallback_pair_id` (self-referential, `use_alter=True`) | Cascades to pairs/scan_logs/scan_events | Dual (user OR admin) | Active while `is_active` | Core money/support entity |
| `ProjectPair` | 640–811 | One image+video marker pair | `image_filename/video_filename`, marker crop/rotation geometry, `processing_status`/`video_processing_status`/`feature_extraction_status` (independent state machines) | `project_id`; `UniqueConstraint(project_id, pair_index)` | Inherits Project owner | uploaded→processing→completed/failed | `is_ready_for_detection` gates scannability |
| `UserLoginActivity` | 911–921 | Login audit trail | `ip_address`, `is_successful` | `user_id` | Per-user | Append-only | Forensic log |
| `AdminActivity` | 927–935 | Admin action audit trail | `activity_type`, `description` | `admin_id` | Per-admin | Append-only | Accountability trail |
| `SystemConfig` | 941–955 | Admin-editable key/value runtime config | `config_key` (unique), `config_value`, `config_type` | `updated_by`→admins | Admin-owned | Overwritten in place | Runtime behavior tuning without redeploy |
| `Organization`/`Workspace`/`WorkspaceMember`/`Experience`/`ExperienceVersion`/`ExperienceVersionTrigger`/`Trigger`/`Asset`/`TriggerAsset`/`RecognitionArtifact`/`ProcessingEvent`/`MigrationCheckpoint` | 1040–1435 | Full parallel multi-tenant schema for the dormant Experience Creator subsystem | legacy-bridge FKs to `Project`/`ProjectPair` exist for a future migration path | — | — | **Confirmed still present but dormant** — every write path gated behind `experience_creator_enabled()` (`feature_flags.py:28`), consumption point `experience_creator.py:36` | Do not describe as live functionality anywhere in the handover pack (PART B.3) |

**HANDOVER NOTE (Database/Migration Guide)**: use this table verbatim as the model reference section; cross-reference PART K's config register for every env var that changes model behavior (capacity, OTP, upload-session TTLs).

## PART E (continued) — Migrations

**VERIFIED**: `migrations/` contains `alembic.ini`, `env.py`, and 7 versioned files under `migrations/versions/`, reconstructed by reading every `revision`/`down_revision` pair directly (not by executing `flask db heads`):

```
3914ece79b88 (baseline_current_schema)
   -> bc5642a86981 (razorpay_id_unique_constraints)
   -> 54a108a17fa7 (capacity_config_and_reservations)
   -> ebeab1cf4ec9 (razorpay_webhook_events)
   -> a73f2c19d8e2 (processing_job_rq_foundation)
   -> 44340c16353c (resumable_upload_sessions)
   -> 0b8fffb4c614 (fallback_video_data_model_and_scan_events)   <- current head
```

**Confirmed single linear chain, no branch points** — each revision id is referenced as `down_revision` by at most one other file.

**Two documentation-drift findings** (both P3, real but non-blocking):
1. `docs/production/database-migration-runbook.md:3-7` documents the chain only through `ebeab1cf4ec9` — missing the 3 newest migrations. **Needs updating before this is handed to the server team as authoritative.**
2. `scripts/production/verify_alembic_state.ps1:31` hardcodes `$expectedHead = "ebeab1cf4ec9"` and will **fail its own head-check** against the real current head `0b8fffb4c614` — this script must be updated before use in the deployment runbook (PART M), or it will produce a false failure signal during deployment.

**Production DB requirement — PostgreSQL, hard-enforced**: `_validate_required_runtime_config()` (`app.py:144-149`) requires `DATABASE_URL` to resolve to a `postgresql` backend when in production mode; any other backend (including MySQL, despite `pymysql` still being a listed dependency) is added to the `missing` list and raises `RuntimeError` at boot (`app.py:166-170`). **Minor doc/comment drift**: `migrations/env.py:55-56,64-66`'s own comments describe the production dialect as "MySQL," which is inconsistent with the actual enforced gate (PostgreSQL-only) — flagged for correction but has no functional effect since env.py's batch-mode logic only special-cases SQLite either way.

**SQLite's role**: dev/test only, selected via `TEST_DATABASE_URL`/`SCANSTORY_TESTING=1` (`app.py:191`); `tests/conftest.py` creates a fresh per-test SQLite file. Alembic's `env.py` batch-mode (recreate-table) is switched on specifically for SQLite.

**Real schema-upgrade procedure — a genuine, worth-flagging tension**: the app's actual runtime boot path calls **`db.create_all()`** (`app.py:988-991` and `app.py:11236-11239`), not `flask db upgrade` — Flask-Migrate is wired only to register the `flask db ...` CLI (comment at `app.py:264-270`: "Alembic does not own schema state yet in this phase"). The *documented* production procedure (`docs/production/database-migration-runbook.md`) is a manual, checklist-driven `flask db heads/history/current` → offline-SQL-review → `flask db upgrade` sequence, with an explicit rule that "existing production database may be stamped at the baseline only after schema verification proves the schema already matches" (runbook lines 22-23). **A fresh production DB that boots via `db.create_all()` would never get an `alembic_version` row unless someone explicitly runs `flask db stamp`** — this reconciliation step must be called out explicitly to the server team in the Database/Migration Guide, it is not automatic.

STATUS: VERIFIED (chain/gate/procedure); the `db.create_all()` vs Alembic-ownership tension is a genuine `SERVER_TEAM_VERIFY` item for first production bootstrap — see PART X.

---

# PART K — Configuration / Environment Register

Full register built from `.env.example` (128 lines), every `os.environ.get`/`os.getenv` call in `app.py`, and `feature_flags.py`. Grouped by category; SECRET column marks values that must live only in the approved secret manager. No real values are reproduced anywhere below.

## Database / Redis / Queue

| NAME | Purpose | Required in | Secret | Default | Failure behavior |
|---|---|---|---|---|---|
| `DATABASE_URL` | Main DB connection | **prod: required, must be postgresql** | Yes | none | Prod: `RuntimeError` at boot if missing/non-postgres |
| `TEST_DATABASE_URL` | Test DB connection | test | No (sqlite path) | safe sqlite default | Runtime DB errors if unset while testing |
| `REDIS_URL` | Redis connection for RQ | **prod: required** | Yes | none | Prod: `RuntimeError` at boot; dev falls back to `fake` queue mode |
| `SCANSTORY_QUEUE_MODE` | `fake`/`inline`/`rq` | **prod: required = `rq`** | No | `fake` | Prod: `RuntimeError` if not `rq` |
| `RQ_QUEUE_NAME` | Queue name | optional | No | `scanstory-processing` | Silent fallback |
| `RQ_DEFAULT_TIMEOUT` | Job timeout (s) | optional | No | `600` | Non-integer → `QueueUnavailable` |
| `RQ_MAX_RETRIES` | Max job retries | optional | No | `3` | Silent fallback |

## Flask / Session / Secret

| NAME | Purpose | Required in | Secret | Default | Failure behavior |
|---|---|---|---|---|---|
| `FLASK_SECRET_KEY` | Session-signing secret | **always required, all envs** | Yes | none (no insecure fallback) | `RuntimeError` at boot in every mode |
| `FLASK_ENV` | Environment name | all | No | unset | Drives production-mode/debug gates |
| `FLASK_DEBUG` | Debug/reloader | dev only | No | `False` | Never auto-enabled |
| `SESSION_COOKIE_SECURE` | Cookie `Secure` flag | **prod: required = true** | No | `False` | Prod: `RuntimeError` if not truthy |
| `SCANSTORY_TESTING` | Marks a test process | test | No | `False` | Governs DB URL selection, disables debug |
| `SCANSTORY_DEV_TESTING` | Enables disposable dev test-user seeding | dev only, **must be 0 in prod** | No | `0` | Prod: `RuntimeError` if `1` |

## SMTP / Email

| NAME | Purpose | Required in | Secret | Default | Failure behavior |
|---|---|---|---|---|---|
| `SMTP_HOST` | SMTP host | **prod required** | No | placeholder in `.env.example` | Boot `RuntimeError` if missing in prod |
| `SMTP_PORT` | SMTP port | prod required | No | `587` (dev fallback) | Boot fails in prod |
| `SMTP_USER` | SMTP auth user | prod required | Yes | none | Boot fails in prod |
| `SMTP_PASS` | SMTP auth password | prod required | Yes | none | Boot fails in prod |
| `MAIL_FROM` | From-address | prod required | No | falls back to `SMTP_USER` but still checked as required key | Boot fails if literally unset in prod |
| `SMTP_TIMEOUT_SECONDS` | SMTP socket timeout | **all envs, always validated** | No | `10.0` | Non-numeric/≤0 → `RuntimeError` at boot regardless of environment |

## Razorpay

| NAME | Purpose | Required in | Secret | Default | Failure behavior |
|---|---|---|---|---|---|
| `RAZORPAY_KEY_ID` | API key id | prod (functional) | Treat as sensitive | `""` | Not boot-checked; payment calls fail at runtime if unset |
| `RAZORPAY_KEY_SECRET` | API secret | prod | Yes | `""` | Same — silent boot, runtime failure |
| `RAZORPAY_WEBHOOK_SECRET` | Webhook HMAC secret, **separate from KEY_SECRET, no fallback between them** | prod (webhook only) | Yes | `""` | **Fails closed** — webhook route rejects all requests when unset, does not skip verification |

## reCAPTCHA

| NAME | Purpose | Required in | Secret | Default | Failure behavior |
|---|---|---|---|---|---|
| `RECAPTCHA_SITE_KEY` | Client-side site key | prod (functional) | No | `""` | Silent — captcha widget just won't work |
| `RECAPTCHA_SECRET_KEY` | Server verification secret | prod (functional) | Yes | `""` | Silent — server verification fails at runtime |
| `RECAPTCHA_MIN_SCORE` | Min acceptable v3 score | optional | No | `0.5` | Silent fallback |

## Uploads / Media / Resumable Upload

| NAME | Purpose | Required in | Secret | Default | Failure behavior |
|---|---|---|---|---|---|
| `UPLOAD_FOLDER` | Listed in `.env.example` | — | No | — | **Dead/unused** — zero references in `app.py`; superseded by `SCANSTORY_STATIC_UPLOADS_DIR`. Documentation drift to fix. |
| `SCANSTORY_DATA_DIR` | Root media/feature data dir | optional (prod-required in practice per PART L) | No | `"data"` | Silent fallback |
| `SCANSTORY_STATIC_UPLOADS_DIR` | Static uploads dir | optional (prod-required in practice) | No | `static/uploads` | Silent fallback |
| `SCANSTORY_ADMIN_DATA_DIR` | Admin media dir | optional (prod-required in practice) | No | `data_admin` | Silent fallback |
| `MAX_IMAGE_UPLOAD_BYTES` | Per-image cap | optional | No | 50 MiB | Silent fallback |
| `MAX_VIDEO_UPLOAD_BYTES` | Per-video cap | optional | No | 1 GiB | Silent fallback |
| `MAX_IMAGE_DIMENSION_PX` | Max image width/height | optional | No | 8000 | Silent fallback |
| `MAX_IMAGE_PIXELS` | Max total pixel count | optional | No | 40,000,000 | Silent fallback |
| `MAX_VIDEO_DURATION_SECONDS` | Optional duration cap | optional | No | disabled (`None`) | Feature off if unset |
| `MAX_CONTENT_LENGTH` | Whole-request body cap | optional, commented out by default | No | uncapped | Must be sized above `MAX_VIDEO_UPLOAD_BYTES × pairs-per-project` per `.env.example` warning, or legitimate uploads get rejected |
| `SCANSTORY_RESUMABLE_CHUNK_MAX_BYTES` | Max bytes/resumable chunk | optional | No | 1 MiB | Must match reverse-proxy body-size limit (PART L) |
| `SCANSTORY_UPLOAD_SESSION_TTL_MINUTES` | Upload session expiry | optional | No | 1440 (24h) | Silent fallback |
| `SCANSTORY_UPLOAD_SESSION_ABANDONED_STALE_MINUTES` | Abandoned-session threshold | optional | No | 120 | Silent fallback |
| `SCANSTORY_UPLOAD_CLEANUP_BATCH_LIMIT` | Cleanup batch size | optional | No | 200 | Silent fallback |

## Capacity

| NAME | Purpose | Required in | Secret | Default | Failure behavior |
|---|---|---|---|---|---|
| `SCANSTORY_INITIAL_CAPACITY_LIMIT` | Seed value for `CapacityConfig.configured_limit` | optional | No | 25 | Silent fallback |
| `SCANSTORY_CAPACITY_RESERVATION_TTL_MINUTES` | Reservation expiry window | optional | No | 30 | Silent fallback |

## Scanner (advisory/display thresholds only — not detection algorithm constants; see PART I)

| NAME | Purpose | Default |
|---|---|---|
| `SCANSTORY_MARKER_MIN_PIXELS` | Minimum marker crop size | 240 |
| `SCANSTORY_VIDEO_RECOMMENDED_SIZE_BYTES` | Advisory size hint | 15 MiB |
| `SCANSTORY_VIDEO_WARNING_SIZE_BYTES` | Advisory warning | 30 MiB |
| `SCANSTORY_VIDEO_RECOMMENDED_DURATION_SECONDS` | Advisory duration | 30 |
| `SCANSTORY_VIDEO_WARNING_DURATION_SECONDS` | Advisory warning | 60 |
| `SCANSTORY_VIDEO_RECOMMENDED_MAX_HEIGHT` | Advisory resolution | 1080 |

## OTP abuse-protection (8 vars, all bounded/validated at parse time)

`SCANSTORY_OTP_EXPIRY_SECONDS` (bounds 60–3600, default 120), `SCANSTORY_OTP_MAX_VERIFY_ATTEMPTS` (1–20, default 5), `SCANSTORY_OTP_LOCK_SECONDS` (60–86400, default 900), `SCANSTORY_OTP_RESEND_MIN_INTERVAL_SECONDS` (0–3600, default 60), `SCANSTORY_OTP_MAX_RESENDS` (0–20, default 3), `SCANSTORY_OTP_RESEND_WINDOW_SECONDS` (60–86400, default 900), `SCANSTORY_OTP_IP_ATTEMPT_LIMIT` (1–500, default 30), `SCANSTORY_OTP_IP_RESEND_LIMIT` (1–200, default 10). None are secrets.

## Security / CSP / HSTS

| NAME | Purpose | Default | Failure behavior |
|---|---|---|---|
| `SECURITY_CSP_ENABLED` | Master CSP on/off | `True` | `0` → no CSP header at all |
| `SECURITY_CSP_ENFORCE` | Report-only vs enforcing | `False` (report-only) | Must stay `0` until browser-QA'd — flipping early risks blocking scanner/OpenCV-WASM/Razorpay/reCAPTCHA per in-code comment |
| `SECURITY_HSTS_ENABLED` | HSTS header | `False` | Only sent over genuine HTTPS even if enabled; enabling before HTTPS is verified can lock out browsers |

## Bootstrap Admin

| NAME | Purpose | Default | Failure behavior |
|---|---|---|---|
| `BOOTSTRAP_ADMIN_ENABLED` | One-time first-admin creation switch | `False`, must be off on every normal boot | If an admin already exists, no-op regardless of flag |
| `BOOTSTRAP_ADMIN_EMAIL` | First admin email | none | Required only when enabled |
| `BOOTSTRAP_ADMIN_PASSWORD` | First admin password | none | Required only when enabled; secret |

## Feature flags (`feature_flags.py`, all 8 verified `False` in code)

`ENABLE_EXPERIENCE_CREATOR`, `ENABLE_TRIGGER_MANAGEMENT`, `ENABLE_PROCESSING_STATUS_UI`, `ENABLE_EXPERIENCE_QR_ASSET`, `ENABLE_EXPERIENCE_PUBLISHING`, `ENABLE_PUBLIC_EXPERIENCE_ROUTE`, `ENABLE_VERSION_ROLLBACK`, `ENABLE_EXPERIENCE_PAUSE` — each is env-overridable individually but **none is documented in `.env.example`** (a documentation gap to fix, low risk since the code default is safe/off).

**Two documentation-drift findings for the Environment & Configuration Guide**:
1. `UPLOAD_FOLDER` in `.env.example` is dead config — never read by `app.py`. Remove or correct in the example file.
2. The 8 `ENABLE_*` feature-flag env-var overrides are undocumented in `.env.example`.

STATUS: VERIFIED (full register); the two drift findings are P3 documentation items (PART X).

---

# PART R — Requirements / Dependency Audit

**VERIFIED — ceiling-only pins confirmed unchanged**: all 27 entries in `requirements.txt` use `<=` with no lower bound and no exact `==` pin anywhere (grep for `==` returns zero matches). Examples: `Flask<=2.3.3`, `Werkzeug<=2.3.8`, `opencv-python<=4.10.0.84`, `sqlalchemy<=2.0.27`, `cryptography<=42.0.7`, `redis<=5.0.8`, `rq<=1.16.2`. This matches `SCANSTORY_V1_GAP_AUDIT.md` SE6 exactly — **no change**.

**VERIFIED — redis/rq are now present** (a prior audit's finding that they were absent is now stale/superseded): `requirements.txt:26-27`.

**Production vs test/dev-only**: all 27 `requirements.txt` entries are production dependencies; `pytest`, `pytest-cov`, `pytest-mock` are correctly isolated in `requirements-dev.txt` only, not duplicated into the production file.

**Windows-only packages**: none found in either requirements file. `windows_rq_worker.py` and `run-tests.ps1` are Windows-flavored **scripts** at the repo root, not pinned dependencies — see PART H/B.5 for their exclusion from shipping.

**Node/build tooling — real, not aspirational**: `package.json` defines a genuine local build step: Tailwind CLI compiles `static/css/tailwind.build.css` from a source file (not a CDN `<script>` tag for this asset), and `esbuild` bundles the `qrcode` npm package into a vendored, minified IIFE (`static/vendor/qrcode/qrcode.min.js`). `package-lock.json` (present, ~63KB) confirms an actually-installed/locked toolchain. **Caveat**: `app.py`'s own CSP directive list still separately allow-lists CDN origins (`cdn.tailwindcss.com`, `cdnjs.cloudflare.com`) used by some templates — the local npm build and CDN-hosted libraries currently coexist, they are not mutually exclusive today.

**HANDOVER NOTE (Environment & Configuration Guide / Server Deployment Guide)**: flag the ceiling-only pin style as a standing reproducibility/security-patch risk requiring a dedicated future pass (not a launch blocker), and note that `npm run build` (from `package.json`) is a real pre-deploy step the server team needs to run (or the pre-built `static/css/tailwind.build.css` / `static/vendor/qrcode/qrcode.min.js` artifacts need to ship as-is) — confirm which approach this release uses before finalizing the deployment runbook.

STATUS: VERIFIED throughout.

---

---

# PART H — Queue / Processing Operations Audit

| # | VERIFIED FACT | EVIDENCE/SOURCE | STATUS |
|---|---|---|---|
| H1 | `rq_worker.py` (34 lines) is committed/tracked. Queue name from `processing_queue.queue_name()`, env `RQ_QUEUE_NAME`, default `"scanstory-processing"`. Redis connection via `REDIS_URL`. Worker refuses to start (`SystemExit`) unless `SCANSTORY_QUEUE_MODE=="rq"` and `REDIS_URL` is set. | `rq_worker.py:14-17,16,27`; `processing_queue.py:61-62,67,144,309`. | VERIFIED |
| H2 | Three-mode queue concept (`fake`/`inline`/`rq`) selected by `SCANSTORY_QUEUE_MODE`; if unset, resolves to `rq` when production-required, `fake` when testing, `rq` if `REDIS_URL` present, else `fake`. | `processing_queue.py:14,25-44`. | VERIFIED |
| H3 | `ProcessingJob` is now genuinely wired into the live upload/reprocess path — not just present as a dormant reference design. `enqueue_project_pair_processing()` is called from `_schedule_project_pair_processing()` (`app.py:1737-1740`), itself called from **5 real call sites**: project edit/reprocess (`app.py:4831,4858`), the main `/upload` route (`app.py:5733`, inside `handle_upload()` at `5313`), the resumable-upload finalize path (`app.py:5966`), and admin upload (`app.py:11013`, inside `admin_handle_upload()` at `10788`). Retry: RQ-level `Retry(max=retry_count, interval=[30,120,300,900])` when in `rq` mode, plus app-level exponential backoff (`30*2**attempt`, capped 900s) in `mark_job_failed()`, flipping to `retrying` vs terminal `failed` by `attempt_count < max_attempts`. | `models.py:1326-1389` (fields); `processing_queue.py:110,136,170,209,216-260,263`; `app.py:57,62,1737-1740,4831,4858,5313,5733,5966,11013,10788`. | VERIFIED — this is a materially different (and much stronger) state than `SCANSTORY_V1_GAP_AUDIT.md` J1/J9's "no durable queue at all, thread-based, silently loses work" finding |
| H4 | `/ready` checks both DB (`SELECT 1`) and, when `queue_mode()=="rq"`, Redis reachability via `redis_ready_check()` (`Redis.from_url(REDIS_URL).ping()`); returns 503 with `{"queue":"unavailable"}` on failure. `/healthz` is pure liveness (no DB/Redis touch, always 200) — correct k8s-style liveness/readiness split. | `app.py:592-596,599-610,613-632`; `processing_queue.py:300-316`. | VERIFIED |
| H5 | Full CLI register — see PART H's companion table already written above (15 commands total, cross-verified independently by both the Security+Payment and Queue+Scanner research passes with identical line numbers). `recover-processing-jobs` (`app.py:2607-2656`) specifically: filters stuck `process_project_pairs` jobs by stale `last_heartbeat_at` past `--older-than-minutes` (default 30), marks `retrying` or terminal `failed` per `attempt_count` vs `max_attempts`, dry-run unless `--apply`. | `app.py:2607-2656` and the full 15-command table earlier in this document. | VERIFIED |
| H6 | `windows_rq_worker.py` — full 5-line content confirmed: a `SimpleWorker` subclass swapping RQ's default `UnixSignalDeathPenalty` (SIGALRM-based, unavailable on Windows) for `TimerDeathPenalty` (a `threading.Timer`-based equivalent). **Confirmed untracked** (`git ls-files` returns nothing for it) and **confirmed unreferenced anywhere in committed code** (`git grep -n "windows_rq_worker"` across all tracked files returns zero matches — not imported by `rq_worker.py`, not referenced by any script, CLI command, or doc). A stray `__pycache__/windows_rq_worker.cpython-310.pyc` exists as evidence of local execution only. | `windows_rq_worker.py` (full file); `git ls-files`/`git grep` results. | VERIFIED — **must be excluded from any staging/production deployment artifact.** Production Linux must use the standard `rq_worker.py` / `rq worker` flow only. |
| H7 | Resumable upload: routes `POST /api/uploads/sessions` (create, `app.py:5979`), `POST /api/uploads/sessions/<id>/chunk` (`6077`), `GET /api/uploads/sessions/<id>` (status, `6216`), `POST /api/uploads/sessions/<id>/finalize` (`6548`), `POST /api/uploads/sessions/<id>/cancel` (`6638`). Exactly-once semantics enforced at **two independent layers**: (a) finalize uses an atomic conditional `UPDATE ... WHERE status='active' AND current_offset=expected_total_size` (or `WHERE status='assembled'`) — a losing/duplicate finalize call gets a conflict response, not re-run assembly (`app.py:6548-6635`); (b) `enqueue_processing_job()` short-circuits (`if not created and job.queue_job_id: return job, False`) backed by a DB-level `UniqueConstraint(project_id, idempotency_key)` (`models.py:1373`) — a job can never be double-enqueued. | `app.py:5979,6077,6216,6548-6635,5948-5965`; `processing_queue.py:170-179`; `models.py:1373`. | VERIFIED — closes `SCANSTORY_V1_GAP_AUDIT.md` U1/U4 |
| H8 | One minor, honestly-flagged open item: `retry_failed_job()` (`processing_queue.py:263`) is imported into `app.py:62` but **never called anywhere** — the only live retry path today is the `recover-processing-jobs` CLI (which sets `status="retrying"` directly) and RQ's own internal `Retry`. Documented in the repo's own `docs/development/wave-7-upload-processing-audit.md:70,286` as a deliberately-deferred "add creator-visible retry route only if it can reuse `retry_failed_job()` safely" item — not a bug, a known future enhancement. | `processing_queue.py:263`; `app.py:62`; `docs/development/wave-7-upload-processing-audit.md:70,286`. | VERIFIED (STILL OPEN, low severity — P3, see PART X) |

**HANDOVER NOTE (Queue/Worker Operations doc)**: production Linux deployment must run the standard `rq worker` process against `rq_worker.py`'s registered queue name; `SCANSTORY_QUEUE_MODE=rq` and `REDIS_URL` are both hard production requirements (PART K/L); `windows_rq_worker.py` is explicitly excluded from any deployment package (H6).

---

# PART I — Scanner Audit

**`SCANNER_ALGORITHM_CHANGED_IN_RELEASE_BATCH = NO`** — verified by direct evidence, not assumption:
- The seven detection/geometry constants are byte-for-byte identical to prior-audit citations, quoted verbatim from current `app.py`: `ORB_MAX_DIM = 1200` (2829), `DETECT_MAX_DIM = 960` (2830), `MIN_GOOD_MATCHES = 8` (2840, comment: "raised from 7 — need more matches before trusting homography"), `RANSAC_REPROJ = 5.0` (2841, "tightened from 8.0 — fewer false inliers"), `MIN_INLIERS_ABS = 8` (2842, "raised from 6 — 5 inliers produced degenerate H"), `MIN_INLIERS_RATIO = 0.30` (2843), `MAX_INLIERS_REQUIRED = 40` (2849). Live usage confirmed at `app.py:8256,8857` (`cv2.findHomography(..., cv2.RANSAC, RANSAC_REPROJ)`), `app.py:3522,8864` (inlier-floor calc), `app.py:3451,3625,8146,8149,8166,8842` (`MIN_GOOD_MATCHES` gate).
- Client-side tracking constants unchanged: `TRACKING_GRACE_MS = 900`, `POSE_HOLD_MS = 500` (and derivatives), `cv.calcOpticalFlowPyrLK(...,(21,21),3)` — all in `templates/user/scanner.html` (lines 2227-5262).
- Direct diff check: `git diff -- templates/user/scanner.html | grep -iE "calcOpticalFlow|POSE_HOLD|TRACKING_GRACE|threshold|inlier|ransac"` → **zero matches**. `static/js/scanner-runtime.js` has **zero diff** in the current working tree at all. The `app.py` diff's hunk headers land exclusively in unrelated functions (`_project_unavailable_response`, `login_required`, `projects_page`, `admin_user_profiles`, `admin_user_scans`, `admin_settings`) — none overlap the 2829-8864 detection block.
- The `templates/user/scanner.html` diff (121 lines, read in full by the research pass) is exclusively a new stylesheet link, CSS class renames, and markup restyling of the fallback/help panels (`ss-scan-btn` design-system classes, `ss-user-scope` body class) — **no `<script>` content changed**.

| # | VERIFIED FACT | EVIDENCE/SOURCE | STATUS |
|---|---|---|---|
| I1 | Camera lifecycle, state machine, and marker-loss recovery live inline in `templates/user/scanner.html`'s `<script>` block; grace/hold timing constants at lines 2227-2505. | `scanner.html:2227-2505,3667,5010`. | VERIFIED |
| I2 | Client-side optical-flow tracking: `cv.calcOpticalFlowPyrLK` at `scanner.html:5262` (21×21 window, 3 pyramid levels); LK-baseline staleness check at `scanner.html:5205`. | Same. | VERIFIED |
| I3 | Server-side ORB feature extraction: `extract_features_multi()` (`app.py:3254`), `make_feature_working_jpeg()` (`app.py:3786`). Homography/inlier scoring at two call sites sharing identical thresholds by design: `app.py:8146-8268` (primary detect/track path) and `app.py:8842-8864` (secondary path — comment at 8268 confirms "uses the exact same MIN_INLIERS_ABS/MIN_INLIERS_RATIO thresholds"). | `app.py:3254,3786,8146-8268,8842-8864`. | VERIFIED |
| I4 | Public scanner endpoints and their rate limits, all via a central `RATE_LIMITS` table and `_check_rate_limit()`: `/scanner/<project_id>` (`7769`), `/detect_init` (`7832`, 45/60s per IP), `/detect_track` (`8784`, 240/60s per IP), session-end (`8446`, 90/60s per IP), fallback-video (`7616`), fallback-event telemetry (`8585`), opencv-telemetry diagnostics (`8708`). | `app.py:284,299-310,7616,7769,7832,8446,8585,8708,8784`. | VERIFIED |
| I5 | Diagnostics: a `?scanner_debug=1` panel (referenced in `gate-jr/cross-device-test-matrix.md:14-16`) surfaces generation/session IDs, camera start/restart counts, good matches, inliers, inlier ratio for on-device troubleshooting. | `gate-jr/cross-device-test-matrix.md:14-16`. | VERIFIED |
| I6 | Fallback/unavailable handling: `#fallbackPanel`/`#recognitionHelpPanel`/`#fallbackVideoPanel` in `scanner.html` (restyled, not logically changed, in the current dirty batch); server-side `_project_unavailable_response()` (`app.py:1568`, also touched in the dirty batch but confirmed unrelated to detection logic). A new styled template `templates/user/project_unavailable.html` was added in this release batch (PART Q) — closing the parity audit's "no styled unavailable page" gap. | `app.py:1568`; `scanner.html` fallback panel markup. | VERIFIED |
| I7 | **Device/browser testing honesty check — corrected.** A prior version of this row claimed no Android/device testing had ever occurred; that blanket claim was **factually wrong** and is corrected here. `gate-jr/cross-device-test-matrix.md` — the structured per-scenario data-collection matrix — is genuinely an **empty template** ("No rows recorded yet — this is a template only... Do not claim real-device or cross-device certification from automated tests alone," lines 3-9,73-74); that specific matrix has in fact never been filled in. Separately from that matrix, **local verification has actually been performed earlier in this project's hardening history**: Chrome desktop, Edge desktop, Brave desktop, and Android Chrome, including Android Chrome over HTTPS specifically for the scanner/camera flow. `gate-j/04-desktop-browser-results.md`'s "Firefox Windows not executed" and "Safari macOS not executed" remain accurate — those two have genuinely never been tested. `gate-j/05-android-results.md`'s "Android testing is blocked"/"No Android browser is certified" framing is stale and superseded by the local Android Chrome verification above; it should not be read as the current state. | `gate-jr/cross-device-test-matrix.md`; project hardening history (local verification predating this audit pass); `gate-j/04-desktop-browser-results.md`. | VERIFIED (corrected) — **LOCAL VERIFIED**: Chrome desktop, Edge desktop, Brave desktop, Android Chrome, and Android Chrome over HTTPS for the scanner/camera flow specifically. **STAGING TEAM VERIFY (genuinely still pending)**: iOS Safari, Firefox (any platform), and the broader staging HTTPS/device matrix generally — none of these three have ever been tested. Carry forward the iOS Safari/Firefox/broader-matrix gap to PART X as `SERVER_TEAM_VERIFY`. |

**HANDOVER NOTE (Scanner Operations doc)**: document the architecture map above without ever re-deriving or re-tuning the constants; explicitly state in the handover pack that local verification already covers Chrome desktop, Edge desktop, Brave desktop, and Android Chrome (including over HTTPS for the scanner/camera flow), while iOS Safari, Firefox, and a real staging HTTPS device matrix remain an outstanding staging-team responsibility.

---

---

# PART S — MNC Handover Document Blueprint

23 eventual handover documents, each mapped to the section(s) of THIS evidence pack that source it, plus missing inputs.

| # | Document | Audience | Purpose | Sourced from (this doc) | Missing inputs / TBDs |
|---|---|---|---|---|---|
| 1 | Executive Handover | Business Owner, Product Owner | One-page narrative: what shipped, what's deferred, verdict | PART A, PART Y | Business framing/tone, not technical |
| 2 | Product Scope & V1 Definition | Product Owner, Support | What V1 includes/defers/gates | PART B | None — fully sourced |
| 3 | System Architecture | App Dev, Server/Infra | Component map, diagram, data flow | PART C | Actual deployed topology (server count, region) — `SERVER_TEAM DECISION REQUIRED` |
| 4 | Environment & Configuration Guide | Server/Infra, App Dev | Full env-var register | PART K | reCAPTCHA domain allow-list, real secret values (never in this pack) |
| 5 | Server Team Deployment Guide | Server/Infra | OS/deps/DB/Redis/proxy/TLS requirements | PART L | OS/Python version pin, CPU/RAM sizing, hosting provider — all `SERVER_TEAM DECISION REQUIRED` |
| 6 | Production Runbook | Server/Infra, App Dev | Ordered deploy sequence | PART M (cites `docs/production/deployment-runbook.md` verbatim) | `[Rollback Authority Role]` name, CI/CD tooling specifics |
| 7 | Incident & Recovery Guide | Server/Infra, App Dev, Support | Per-category incident response | PART N (cites `docs/production/incident-response.md`) | SMTP-failure procedure (genuine gap, PART X) |
| 8 | Backup/Restore Guide | Server/Infra, DBA | What/how/how-often | PART O (cites `docs/production/backup-restore-runbook.md`) | Retention duration, backup vendor/tooling — `BACKUP RETENTION = TBD BY BUSINESS/SERVER TEAM` |
| 9 | Security & Access Control Guide | Security, App Dev | Auth/CSRF/OTP/headers/rate-limit posture | PART F | IP-based admin-login rate limiting (currently absent, P2 candidate) |
| 10 | Database/Migration Guide | DBA, App Dev | Models, migration chain, upgrade procedure | PART E | First-production-bootstrap Alembic-stamp decision (genuine tension identified in PART E) |
| 11 | Queue/Worker Operations | Server/Infra, App Dev | RQ/Redis operations, CLI register | PART H | Worker process count/supervision — `SERVER_TEAM DECISION REQUIRED` |
| 12 | Payment & Subscription Operations | Payments/Finance, Support | Lifecycle, webhook, reconciliation | PART G | Live Razorpay webhook staging certification (outstanding, PART X) |
| 13 | Scanner Operations | App Dev, Support | Architecture, diagnostics, testing status | PART I | Real-device certification matrix (outstanding, `SERVER_TEAM_VERIFY`) |
| 14 | Monitoring & Health Checks | Server/Infra | `/healthz`/`/ready` contracts, alert targets | PART P | Actual monitoring platform integration — none exists yet |
| 15 | Release/Rollback Guide | App Dev, Server/Infra | Rollback triggers/procedures | PART M/N (cites `docs/production/rollback-runbook.md`) | `[Rollback Authority Role]` |
| 16 | Admin Manual | Support/Ops, Super Admin | Step-by-step admin panel usage | PART T | — pending PART T completion below |
| 17 | User Manual | Customer Support, end users | Step-by-step end-user procedures | PART U | — pending PART U completion below |
| 18 | Support & Maintenance Model | Support, Business Owner | Who fixes what, SLAs by tier | PART W (RACI) | Actual support tiering/hours — `BUSINESS_DECISION_REQUIRED` |
| 19 | SLA/Support Matrix | Business Owner, Customers | Uptime/response commitments | none in this repo | Entirely `BUSINESS_DECISION_REQUIRED` — no SLA text exists anywhere in code/docs |
| 20 | Ownership/RACI | All roles | Who owns what | PART W | Real names/org chart — placeholders only |
| 21 | Known Limitations & Deferred V1 | Product Owner, Business Owner | Honest scope boundary list | PART B.3, PART X | — fully sourced |
| 22 | Budget & Cost Model | Business Owner, Finance | Cost-input register | PART V | All vendor pricing — `TBD — COMMERCIAL INPUT REQUIRED` |
| 23 | Sign-off Checklist | Business Owner, Server/Infra, App Dev | Formal go/no-go checklist | PART M (deployment stop conditions), PART Y | Actual sign-off signatures |

---

# PART V — Budget/Cost-Evidence Register

No prices are invented anywhere below. Every row states WHAT must be budgeted per repository evidence; vendor pricing is uniformly `TBD — COMMERCIAL INPUT REQUIRED`.

## One-time

| Category | Resource | Why required | Usage/scaling driver | Owner | Status |
|---|---|---|---|---|---|
| Development | This hardening project's engineering effort (already incurred) | Payment idempotency, webhook, capacity, RQ, resumable upload, security hardening all built | N/A (sunk) | App Development | Historical — not a forward budget item |
| QA/hardening | Real-device certification for iOS Safari, Firefox, and the broader staging device matrix — **not yet performed** per PART I (Chrome/Edge/Brave desktop and Android Chrome, incl. over HTTPS for the scanner/camera flow, are already locally verified) | Scanner has local coverage on the above browsers/devices but no iOS Safari, Firefox, or formal staging device-farm coverage yet | Number of remaining device/browser combos to certify | QA / App Dev | `TBD — COMMERCIAL INPUT REQUIRED` (device lab or cloud device-farm cost) |
| Deployment setup | Initial server/PostgreSQL/Redis provisioning, reverse proxy, TLS cert issuance | PART L requirements | One-time per environment (staging + production) | Server/Infra | `TBD — COMMERCIAL INPUT REQUIRED` |
| Handover/docs | Finalizing the 23 documents in PART S from this evidence pack | Business requirement (this task) | One-time | App Development | In progress (this document) |
| Training | Admin/support staff onboarding to the admin panel (PART T) | New support staff need the Admin Manual | Number of staff | Support/Ops | `TBD — COMMERCIAL INPUT REQUIRED` |
| Initial infra | First PostgreSQL/Redis/app-server provisioning cost | PART L | Hosting provider choice | Server/Infra | `TBD — COMMERCIAL INPUT REQUIRED` |

## Recurring (monthly)

| Category | Resource | Why required | Scaling driver | Owner | Status |
|---|---|---|---|---|---|
| App server | Gunicorn/WSGI host running `app.py` | PART C/L | Traffic volume, capacity cap (25 accounts default) | Server/Infra | `TBD — COMMERCIAL INPUT REQUIRED` |
| PostgreSQL | Production DB (hard-required, PART E) | Schema + payment/quota data | Row/table growth, connection count | Server/Infra / DBA | `TBD — COMMERCIAL INPUT REQUIRED` |
| Redis | Production queue backend (hard-required in `rq` mode, PART H) | RQ job durability | Job volume | Server/Infra | `TBD — COMMERCIAL INPUT REQUIRED` |
| Media storage | `SCANSTORY_DATA_DIR`/`SCANSTORY_ADMIN_DATA_DIR` — local filesystem today, no object-storage/CDN found in code | Uploaded images/videos/features/QR codes | Number of projects × pair size | Server/Infra | `TBD — COMMERCIAL INPUT REQUIRED`; note: no S3/GCS/Azure Blob integration found anywhere in this repo — storage is local-disk today |
| Backups | DB + media backup storage (daily + pre-deploy, PART O) | Verified requirement, no tooling/vendor specified in repo | Data volume, retention (TBD) | Server/Infra | `TBD — COMMERCIAL INPUT REQUIRED` |
| Bandwidth | Video/media egress to scanner viewers + landing-page assets | Public scanner is anonymous/unauthenticated, unbounded viewer count per project | View volume | Server/Infra | `TBD — COMMERCIAL INPUT REQUIRED` |
| Domain/DNS | Production domain | PART L (`SERVER_TEAM DECISION REQUIRED` — no domain defined in repo) | Flat | Business/Server | `TBD — COMMERCIAL INPUT REQUIRED` |
| TLS | Certificate issuance/renewal | PART L requirement (HTTPS mandatory for cookies/HSTS/Razorpay webhook) | Flat or automated (Let's Encrypt has no direct cost but needs infra) | Server/Infra | `TBD — COMMERCIAL INPUT REQUIRED` |
| SMTP | Outbound email provider (`SMTP_HOST/PORT/USER/PASS`) | OTP/verification/reset/payment emails — required for prod boot | Email volume (registration + OTP resend + payment) | Server/Infra / Email provider | `TBD — COMMERCIAL INPUT REQUIRED` |
| Monitoring/logging | External uptime/TLS-expiry/log-aggregation platform | PART P — none of this exists in-repo today, all external per this audit | Log/metric volume | Server/Infra | `TBD — COMMERCIAL INPUT REQUIRED` |
| Payment-gateway charges | Razorpay transaction fees | Every successful order incurs a gateway fee | Transaction volume × plan price | Payments/Finance | `TBD — COMMERCIAL INPUT REQUIRED` (Razorpay's own fee schedule, not in this repo) |
| Maintenance/support | Ongoing app maintenance (dependency patching given ceiling-only pins, PART R) | Security-patch cadence needed given no lower-bound pins | Engineering hours | App Development | `TBD — COMMERCIAL INPUT REQUIRED` |

## Variable

| Category | Driver | Owner | Status |
|---|---|---|---|
| Bandwidth | Scanner view volume (anonymous, unbounded per project) | Server/Infra | `TBD — COMMERCIAL INPUT REQUIRED` |
| Media storage growth | New projects × pair count × video size (up to `MAX_VIDEO_UPLOAD_BYTES`, default 1 GiB per video) | Server/Infra | `TBD — COMMERCIAL INPUT REQUIRED` |
| Payment transaction charges | Razorpay per-transaction fee × order volume | Payments/Finance | `TBD — COMMERCIAL INPUT REQUIRED` |
| Email volume | OTP sends + resends (bounded by `SCANSTORY_OTP_MAX_RESENDS`=3/window per account) + payment/verification emails | Server/Infra | `TBD — COMMERCIAL INPUT REQUIRED` |
| Worker capacity | RQ worker process count vs. upload/reprocess job volume | Server/Infra | `TBD — COMMERCIAL INPUT REQUIRED` |

## Staff

| Role | Driver | Status |
|---|---|---|
| Server/DevOps | PostgreSQL/Redis/proxy/TLS/backups/monitoring ownership (PART L/N/O/P) | `BUSINESS_DECISION_REQUIRED` (headcount/contractor) |
| App maintenance | Dependency patching (ceiling-only pins, PART R), bug fixes, feature work | `BUSINESS_DECISION_REQUIRED` |
| Support/admin ops | Day-to-day admin panel usage (PART T), user support (PART U) | `BUSINESS_DECISION_REQUIRED` |
| QA/release support | Regression testing, real-device certification (PART I gap) | `BUSINESS_DECISION_REQUIRED` |

## Future-scale (not needed at V1's 25-account capacity default, but worth pre-registering)

| Item | Trigger | Status |
|---|---|---|
| Extra app instances | Traffic beyond single-Gunicorn-host capacity; note the rate limiter is process-local (PART F/L) — horizontal scale requires a Redis-backed limiter first | `TBD — COMMERCIAL INPUT REQUIRED` |
| Managed/HA PostgreSQL | Beyond a single DB instance | `TBD — COMMERCIAL INPUT REQUIRED` |
| Redis HA | Beyond a single Redis instance (queue availability risk today, PART H) | `TBD — COMMERCIAL INPUT REQUIRED` |
| Object storage / CDN | Local-disk media storage (`SCANSTORY_DATA_DIR`) does not scale/replicate by itself | `TBD — COMMERCIAL INPUT REQUIRED` |
| Worker scaling | Multiple RQ worker processes/hosts | `TBD — COMMERCIAL INPUT REQUIRED` |
| Observability platform | APM/structured-logging platform — `docs/production/README.md` marks `LOG_LEVEL`/`STRUCTURED_LOGGING_ENABLED` as "future, not yet active" | `TBD — COMMERCIAL INPUT REQUIRED` |

---

# PART W — RACI / Ownership Input Register

Role placeholders only — no names invented. R=Responsible, A=Accountable, C=Consulted, I=Informed.

| Activity | Business Owner | Product Owner | App Development | Server/Infra | DBA | Security | Payments/Finance | Support | Super Admin | Operations |
|---|---|---|---|---|---|---|---|---|---|---|
| Product scope decisions (PART B) | A | R | C | I | I | I | C | I | I | I |
| Feature flag activation (Experience Creator) | A | R | R | I | I | C | I | I | I | I |
| Code changes to `app.py`/`models.py` | I | C | R/A | I | C | C | I | I | I | I |
| Scanner algorithm changes | I | C | R/A (with explicit sign-off per project rule) | I | I | I | I | I | I | I |
| Server provisioning (PostgreSQL/Redis/proxy/TLS) | I | I | C | R/A | C | C | I | I | I | I |
| Database schema migrations | I | I | R | C | R/A | I | I | I | I | I |
| Backup execution & retention policy | A | I | I | R | R | I | I | I | I | C |
| Security posture (CSRF/CSP/rate-limit/secrets) | I | I | R | C | I | R/A | I | I | I | I |
| Razorpay integration & webhook secret custody | I | I | R | C | I | C | R/A | I | I | I |
| Refund/chargeback handling (currently unsupported — PART G) | A | C | I | I | I | I | R | I | I | I |
| Admin panel day-to-day operation | I | I | I | I | I | I | I | R | A | C |
| Incident response execution (PART N) | I | I | R | R | C | C | I | I | I | A |
| Monitoring/alerting platform selection & ops | I | I | C | R/A | I | C | I | I | I | C |
| Release/deployment execution (PART M) | I | I | R | R/A | C | C | I | I | I | I |
| Rollback decision authority | A | C | C | R | I | I | I | I | I | I |
| SLA/support-tier definition | A | C | I | I | I | I | I | R | I | I |
| Budget/vendor selection (PART V) | A/R | C | I | C | I | I | C | I | I | I |

**HANDOVER NOTE**: this table intentionally leaves `[Rollback Authority Role]` and every "who exactly" question as a placeholder — the repo's own runbooks (`docs/production/*.md`) use the identical bracketed-placeholder convention, confirming this is a deliberate, already-established pattern in this project, not an oversight introduced by this audit.

---

---

# PART D — Route / Surface Inventory

~117 `@app.route(` handlers in `app.py`, plus a registered blueprint (`experience_creator_bp`, ~25 routes, entirely feature-flag-gated) and 15 CLI commands (PART H).

| Group | ~Count | Representative routes | Auth / significance |
|---|---|---|---|
| PUBLIC | ~12 | `/` (landing, `4017`), `/terms`, `/privacy`, `/blog`, `/blog/<slug>`, `/pricing`, `/contact`, `/sitemap.xml`, `/robots.txt`, `/faqs` | No auth; marketing/legal/SEO |
| AUTH | ~8 | `/register` (`4866`), `/verify-email/` (`4983`), `/resend-otp/` (`5019`), `/login/` (`5049`), `/logout/` (`5195`), `/forgot-password/` (`5203`), `/reset-password/` (`5238`) | Pre-auth by design; rate-limited on register/login |
| USER (self-service) | ~10 | `/dashboard` (`4318`, `@login_required`), `/profile` (`4609`, GET-only), `/projects` (`4624`, `@login_required`), `/projects/<id>/edit` (`4726`/`4738`), `/projects/delete/<id>` (`4708`) | Ownership-checked pattern (`if project.owner_user_id != user.id: abort(404)`, e.g. `app.py:6726`) |
| PROJECT/MEMORY | ~6 | `/create-project` (`5280`, `@login_required @enforce_subscription`), `/project/<id>` (`6709`), `/project/<id>/fallback-pair` (`6761`), `/project/<id>/reprocess` (`4839`) | `enforce_subscription` checks plan limits before create |
| UPLOAD | 5 (resumable) + 1 (legacy) | `/api/uploads/sessions` (create, `5979`), `.../chunk` (`6077`), `.../<id>` (status, `6216`), `.../finalize` (`6548`), `.../cancel` (`6638`); legacy `/upload` (`5313`, `@login_required @enforce_subscription`) | Resumable routes resolve identity internally via `_upload_identity()` (supports both user and admin uploaders), not a route decorator |
| SCANNER (public) | 9 | `/scanner/<project_id>` (`7769`, no decorator), `/detect_init` (`7819`, `@csrf.exempt`), `/detect_track` (`8775`, `@csrf.exempt`), `/api/scanner/session/end` (`8446`, `@csrf.exempt`), `/api/scanner/<id>/fallback-event` (`8585`, `@csrf.exempt`), `/api/scanner/<id>/fallback-video` (`7616`), `/api/scanner/<id>/opencv-telemetry` (`8708`, `@csrf.exempt`), media routes `/video/<pid>/<iid>`, `/image/<pid>/<iid>`, `/qr/<filename>` (`7576/7592/7607`) | Intentionally anonymous/unauthenticated; all CSRF exemptions individually justified inline (no session/cookie to bind a token to); **inconsistency noted**: `/scanner/<id>` returns a bare unstyled `"Project not found"` (default HTTP 200) when the ID doesn't exist at all, vs. the new styled `project_unavailable.html` 404 when the project exists but is suspended (`is_active=False`) — a real, minor UX inconsistency worth a follow-up ticket, not a blocker |
| PAYMENT | ~7 | `/subscribe` (`6799`, `@login_required`), `/create-razorpay-order` (`6924`, `@login_required`, reserves capacity atomically before order creation), `/verify-payment` (`7056`, `@login_required`, idempotent), `/payment-success` (`7516`), `/payment-failed` (`7537`) | Capacity gate rejects with `capacity_full_rejection` log entry if slots exhausted |
| WEBHOOK | 1 | `/webhooks/razorpay` (`7223`, POST, `@csrf.exempt`) | Session-independent; authenticity via HMAC-SHA256 signature only (PART F/G) |
| ADMIN | ~48 | `/admin/login` (`8955`), `/admin/dashboard` (`9270`), `/admin/users` (`9351`), `/admin/projects/<id>/delete` (`10384`, destructive), `/admin/plans/<id>/delete` (`9810`, destructive), `/admin/capacity` (`10643`), `/admin/webhook-events` (`10680`, read-only) | See super-admin breakdown below; destructive routes: `admin_delete_project`, `admin_delete_plan`, `admin_delete_admin`, `admin_delete_own_project` |
| HEALTH | 2 | `/healthz` (`592`), `/ready` (`613`) | Unauthenticated by design (PART H/P) |
| OPERATIONS/CLI | 15 | See PART H's full table | `flask <command>`, out-of-band, not HTTP routes |
| DEFERRED EXPERIENCE CREATOR | ~25 | `experience_creator_bp` (registered `app.py:274`): `/experiences` (`160`), `/experiences/new` (`211`), `/experiences/<id>` (`241`), `/e/<public_key>` (public viewer, `375`), `/experiences/<id>/triggers/new` (`389`) | **Every route calls `_gate_enabled()`/`_require_user()`, which `abort(404)`s unless `ENABLE_EXPERIENCE_CREATOR=True`** (default `False`). Entirely inert in the current deployment configuration. |

**Super-admin-only routes** (all via `@require_admin_permission("superadmin.*")`, logged `access_denied` on rejection): admin management (`superadmin.admins.manage`), plan CRUD+toggle (`superadmin.plans.manage`), settings (`superadmin.settings.manage`), capacity (`superadmin.capacity.manage`), activity logs (`superadmin.audit.view`), subscriptions (`superadmin.operations.view`), extend/increase-limits/deactivate subscription (`superadmin.settings.manage`), project delete (`superadmin.repair.execute`).

**Dead-code note**: `super_admin_required` (`app.py:1938-1939`, an alias for `require_admin_permission("superadmin.admins.manage")`) exists but is used as a decorator **nowhere** (0 matches) — all gating now goes through `require_admin_permission(...)` calls directly. Harmless, but worth removing in a future housekeeping pass.

STATUS: VERIFIED throughout.

---

# PART J — Frontend / UX Audit

## J.1 Template inventory

**User templates (25 files, all confirmed LIVE — each has ≥1 `render_template` call in `app.py`)**: `landing.html`, `register.html`, `login.html`, `verify_email.html`, `forgot_password.html`, `reset_password.html`, `email_verification.html`, `dashboard.html`, `profile.html`, `projects.html`, `project_preview.html`, `edit_project.html`, `user_create_project.html`, `success.html`, `scanner.html`, `project_unavailable.html` (new), `subscribe.html`, `payment_success.html`, `payment_success_email.html`, `contact.html`, `blog.html`, `blog_articles/article.html`, `terms.html`, `privacy_policy.html`. Plus 6 `experiences/*.html` templates, LIVE only if `ENABLE_EXPERIENCE_CREATOR=True` (currently `False` — see PART B.3).

**Admin templates (28 files)**, split by which nav pattern they use:
- **Extend `admin/base.html`** (6): `dashboard.html`, `add_admin.html`, `edit_admin.html`, `my_projects.html` (redirect target), `capacity.html`, `webhook_events.html` (new).
- **Hardcode their own sidebar, INCLUDING Subscriptions+Activity Logs links** (8): `users.html`, `scans.html`, `subscriptions.html`, `user_scans.html`, `user_profiles.html` (redirect target), `view_user.html`, `view_payment.html`, `activity_logs.html`.
- **Hardcode their own sidebar, MISSING Subscriptions/Activity Logs/Capacity/Webhook-Events links entirely** (8): `projects.html`, `payments.html`, `settings.html`, `plans.html`, `add_plan.html`, `edit_plan.html`, `project_preview.html`, `view_project.html`. **From any of these 8 pages an admin has no sidebar path to Capacity or Webhook Events** unless they already know the URL or came from Dashboard/a base.html-extending page first.
- Auth/utility pages (no sidebar applicable): `login.html`, `forgot_password.html`, `reset_password.html`, `reset_password_email.html` (email body), `manage_admins.html`.

## J.2 The four new templates from the release batch — verified real and wired

| Template | Route | Permission | Finding |
|---|---|---|---|
| `templates/admin/capacity.html` | `/admin/capacity` (GET/POST, `app.py:10643-10674`) | `superadmin.capacity.manage` | Real. GET shows `_capacity_state_snapshot()`; POST edits only `configured_limit`/`enabled`; never writes `consumed_count` directly (stays derived from `PaymentReservation` via the atomic helpers). Logged as `capacity_config_update`. Closes `SCANSTORY_V1_FEATURE_PARITY_AUDIT.md` C2. |
| `templates/admin/webhook_events.html` | `/admin/webhook-events` (GET, `app.py:10680-10706`) | `admin.payments.view` | Real, read-only, paginated, optional `order_id` filter. Deliberately never renders raw payload/signature/secrets (inline comment). Closes parity doc C3. |
| `templates/admin/reset_password_email.html` | Rendered inside `send_admin_password_reset_email()` (`app.py:1832-1841`), called from `admin_forgot_password()` | n/a (email body) | Real — **fixes the parity doc's P0 crash bug** (every admin forgot-password request previously raised `TemplateNotFound`). |
| `templates/user/project_unavailable.html` | Returned via `_project_unavailable_response()` (`app.py:1567-1572`), used by `scanner()`, `serve_video`, `serve_image`, `serve_qr` and their admin equivalents (7 call sites) | n/a (public) | Real, styled 404 body for suspended/unavailable projects. Closes parity doc C9. |

## J.3 `static/js/nav.js` and `.ss-user-scope`/`.ss-admin-scope` — what they actually fix

- **`static/js/nav.js` (53 lines) does NOT address the admin-nav duplication problem.** It is a **user-side-only** mobile drawer/bottom-sheet toggle utility ("Shared mobile menu behavior for ScanStory user pages"), wired via `#mobileMenuToggle`/`#mobileMenuPanel` into 21 `templates/user/*.html` files. It has nothing to do with the admin sidebar.
- **`templates/admin/base.html` (420 lines) partially, but does not fully, fix the admin-nav problem.** Its own sidebar (lines 290-374) now includes Subscriptions, Payments, Capacity (permission-gated), Settings, and Activity Logs, with an explicit "canonical Projects entry" comment referencing the earlier `fix/admin-navigation-routing` work. **But only 6 of 28 admin templates actually extend `admin/base.html`** — the other 22 still hardcode their own sidebar copy, and 8 of those 22 are missing the Capacity/Webhook-Events/Subscriptions/Activity-Logs links entirely (J.1 above). **Conclusion: the parity audit's "two competing, out-of-sync admin nav systems" finding is still structurally accurate** — this release batch improves `base.html` itself and fixes reachability for the pages that already used it, but does not consolidate the other 22 templates onto one shared nav partial. This is the single most actionable remaining UX finding from this entire audit (see PART X).
- **`.ss-admin-scope`/`.ss-user-scope` dark-mode dropdown fixes are real and broadly applied**, confirmed in `static/css/design-system.css:493-729` — both override Bootstrap's default light-theme select/dropdown styling so dark-mode controls get readable text instead of white-on-white. Applied on 20 of 22 non-base.html admin `<body>` tags and 21 user templates (the base.html-extending admin pages inherit it from `base.html`'s own `<body class="ss-admin-scope">`).

## J.4 Dead templates — resolved from "orphaned render" to "clean redirect"

**`admin/my_projects.html` and `admin/user_profiles.html` are still present on disk but are now genuinely dead as templates** — both routes are pure redirects, not renders:
- `/admin/my-projects` (`app.py:9340-9350`) → `redirect(url_for("admin_projects", owner_type="admin"))`, docstring: "Legacy route, kept for backward compatibility... Redirect directly rather than maintaining two separate project-list implementations/templates."
- `/admin/user-profiles` (`app.py:10138-10158`) → `redirect(url_for("admin_users", ...))`, forwarding query params so old bookmarks keep working; same "kept for backward compatibility" docstring pattern.
- Grep for `render_template("admin/my_projects.html"` / `"admin/user_profiles.html"` across `app.py` returns **zero hits** — this is a genuine fix (converted from "orphaned template a route actually renders" to "retired-route redirect pattern"). The two `.html` files are harmless unused artifacts on disk, safe to delete in a future housekeeping pass but not a functional risk.

**HANDOVER NOTE (Admin Manual + Known Limitations doc)**: the single most valuable remaining UX fix — beyond this release's scope per the "no redesign" instruction, but worth flagging for a fast-follow — is consolidating the 22 non-base.html admin templates onto one shared nav partial, which would durably close the reachability/drift problem instead of requiring every future admin page to remember to add every link everywhere.

---

# PART T — Admin Manual Source Capture

Factual, current-state procedures only (no fabricated actions):

- **Login**: `/admin/login`. Generic "Invalid email or password" on any failure (no account enumeration). Failed attempts tracked per-email; lockout returns HTTP 429 with the same generic message during the lockout window (DB-persisted, PART F.6).
- **Dashboard**: `/admin/dashboard` — shows the logged-in admin's own created projects (pair/scan counts) plus site-wide user stats.
- **User management**: list with status/plan/search filters (`/admin/users`); block/unblock (POST toggle-block); detail view; admin-initiated password reset and trial extension, both POST, both `admin.users.manage`.
- **Project management**: list with search/owner-type/readiness filters; view detail; suspend/restore (`admin.projects.suspend`); **delete is now UI-wired** (`view_project.html`, confirm dialog, "cannot be undone" warning), `superadmin.repair.execute`.
- **Scans**: overview + per-user detail. **Update scan limit, grant extra scans, and lock scanner are all now UI-wired** in `user_scans.html` (forms to `admin_update_scan_limit`/`admin_grant_extra_scans`/`admin_lock_user_scanner`, all `admin.users.manage`) — closes the parity audit's prior "zero UI" finding for these three actions.
- **Plans**: add/edit/delete, and **toggle-status is now UI-wired** (`plans.html`, form to the toggle route). All `superadmin.plans.manage`.
- **Subscriptions**: list (`superadmin.operations.view`); per-order extend/increase-limits/deactivate, all POST, `superadmin.settings.manage`.
- **Payments**: list + detail (`admin.payments.view`). **Refund and receipt-resend are explicitly disabled with honest tooltips** ("Refund processing is not available in this admin package" / "Receipt resend is not available in this admin package") — no backend route exists for either. Do not describe a refund capability anywhere in the final Admin Manual.
- **Webhook events (new)**: `/admin/webhook-events` (`admin.payments.view`), read-only, paginated, filterable by order id; never shows raw payload/signature.
- **Admins**: list/add/edit/delete/toggle-status, all `superadmin.admins.manage`, all UI-wired with last-superadmin/self-protection (per prior audits, structurally unchanged).
- **Capacity (new)**: `/admin/capacity` (`superadmin.capacity.manage`) — view the same snapshot `flask capacity-status` produces; edit `configured_limit`/`enabled` only.
- **Settings**: only 3 fields are functional (`free_trial_projects`, `free_trial_scans`, `free_trial_days`). **The other 10 fields the parity audit called "dead" are now rendered `disabled`** in the HTML, and the POST handler no longer reads/writes them at all — an explicit, deliberate fix (code comment: "a save can never silently overwrite a previously-set value with a disabled input's default"). They remain visible but inert, not removed — describe them as "reserved/inactive" in the final manual, not as working controls.
- **Activity logs**: `/admin/activity-logs` (`superadmin.audit.view`) — reachable from base.html and 8 of the other hardcoded sidebars, but absent from 8 others (J.1) — note the reachability caveat in the final manual until the nav consolidation (PART J.3) lands.
- **Admin password reset (self-service)**: forgot-password → OTP emailed via the now-working `reset_password_email.html` → reset-password (OTP + new password, min 8 chars).
- **Logout**: clears session, logs a `logout` activity entry.

---

# PART U — User Manual Source Capture

- **Registration**: `/register` — email uniqueness, password match/length (min 6 chars), reCAPTCHA v3, creates the account on the trial plan with a `TrialDetails` row, sends a verification OTP.
- **Email verification/OTP**: 2-minute-expiry OTP; verify and resend both available.
- **Login**: `/login/` — if an admin/superadmin session already exists, the user is routed to the admin dashboard instead of the user login form.
- **Forgot/reset password**: OTP-based, reuses the same email-verification template pattern (explicit code comment documents the reuse).
- **Dashboard**: `/dashboard`, login required.
- **Create Memory (project creation)**: `/create-project`, blocked with a flash message + redirect if plan limits are exceeded (checked server-side before the form is even shown as usable).
- **Image/video upload**: two paths coexist — legacy single-shot `/upload`, and the resumable session API (create → chunk → finalize/cancel). Auth for the resumable API is resolved per-request (supports both user and admin uploaders on the same endpoints), not via a route decorator.
- **Processing state visibility**: the projects list computes per-project ready/failed/processing rollups server-side, with a `status` filter and free-text search.
- **My Stories/projects list**: `/projects` — search and status filter are both real, server-side.
- **View/QR/Edit/Fix/Delete**: View is ownership-checked (`abort(404)` on mismatch); QR download is a dedicated route; Edit is GET+POST; "**Fix**" in the UI is the **Reprocess** action (button class `btn-reprocess`); Delete is a dedicated POST route.
- **Subscription/payment**: plan list → order creation (capacity slot reserved atomically before the order exists; a flash message on capacity-full) → Razorpay checkout → idempotent verification → success/failed page.
- **Scanner usage**: public URL, no auth. A nonexistent project ID currently returns a bare unstyled "Project not found" (HTTP 200 — an inconsistency worth a follow-up ticket, see PART D); a suspended/inactive project now returns the new styled unavailable page (404).
- **Scanner retry/fallback**: fallback video resolves after a recognition timeout or camera failure; fallback/analytics events are recorded separately from successful-scan counts (PART E, `ScanEvent` vs `ScanLog`); the creator can designate/clear a project's fallback pair, server-validated so a pair from a different project can never be selected.
- **Profile**: `/profile` is **GET-only — no self-service edit/password-change form exists anywhere** (zero `<form>` tags in the template, no POST route). Describe this honestly in the User Manual as a read-only profile view, not an editable one.
- **Contact**: `/contact` form, sent via SMTP.
- **Logout**: clears session.

---

# PART X — Final Release-Risk Register

## P0 (none found)

No P0 (product-blocking, ship-stopping) findings were identified against current HEAD + the uncommitted release batch. The two prior Critical money-path defects (payment non-idempotency, no capacity check) are both closed and test-covered (PART G). The prior admin-panel P0 crash bug (missing password-reset email template) is closed (PART J/T).

## P1

| # | Finding | Evidence | Recommendation |
|---|---|---|---|
| P1-1 | Admin nav is still structurally two systems — 22 of 28 admin templates hardcode their own sidebar, 8 of those 22 entirely omit links to Capacity/Webhook-Events/Subscriptions/Activity-Logs. | PART J.1/J.3 | Consolidate onto one shared nav partial in a fast-follow pass (explicitly out of scope for "no redesign" in this release, but the highest-value next fix) |
| P1-2 | Live Razorpay webhook delivery to a real public HTTPS staging endpoint has never been exercised — only mocked/simulated requests are covered by automated tests. | `docs/production/razorpay-certification.md` "Not Yet Certified" section; PART G | `SERVER_TEAM_VERIFY` — run the W1-W12 staging checklist before treating webhook reconciliation as production-ready |
| P1-3 | No real iOS Safari or Firefox testing has ever been performed for the scanner, and the structured cross-device test matrix has never been filled in. (Local verification has been performed for Chrome desktop, Edge desktop, Brave desktop, and Android Chrome, including over HTTPS for the scanner/camera flow — see PART I.7, corrected.) | `gate-jr/cross-device-test-matrix.md`, `gate-j/04-desktop-browser-results.md`; PART I | `SERVER_TEAM_VERIFY` — iOS Safari, Firefox, and a real staging HTTPS device matrix required before public launch |

## P2

| # | Finding | Evidence | Recommendation |
|---|---|---|---|
| P2-1 | Admin login has no IP-based rate limit (only per-account DB-persisted lockout). | PART F.5 | Add IP-based throttling alongside the existing lockout in a fast-follow pass |
| P2-2 | `/scanner/<id>` returns a bare unstyled "Project not found" (HTTP 200) for a nonexistent project ID, inconsistent with the new styled 404 for a suspended project. | PART D/U | Small follow-up fix, not a blocker |
| P2-3 | SMTP outage has no dedicated incident-response procedure or CLI recovery tool (unlike DB/Redis/payment/capacity, which all have one). | PART N | Add an SMTP-failure runbook entry |
| P2-4 | `retry_failed_job()` is imported but never called anywhere — a documented, deliberately-deferred creator-visible retry route. | PART H.8 | No action required for V1; tracked as a known future item in the repo's own `docs/development/wave-7-upload-processing-audit.md` |
| P2-5 | `docs/production/database-migration-runbook.md` and `scripts/production/verify_alembic_state.ps1` are stale (document/expect migration head `ebeab1cf4ec9`; real head is `0b8fffb4c614`). | PART E | Update both before using them in the actual deployment runbook — `verify_alembic_state.ps1` will otherwise produce a false failure signal |

## P3

| # | Finding | Evidence |
|---|---|---|
| P3-1 | One plaintext user email printed via `print()` on every scan-attribution call (`app.py:8510`). | PART F.13 |
| P3-2 | Dependency pins remain ceiling-only (`<=`, no lower bound) across all 27 `requirements.txt` entries. | PART F.15/R |
| P3-3 | `UPLOAD_FOLDER` in `.env.example` is dead config (never read); the 8 `ENABLE_*` feature flags are undocumented in `.env.example`. | PART K |
| P3-4 | `super_admin_required` decorator function exists but is used nowhere (dead code). | PART D |
| P3-5 | `admin/my_projects.html`/`admin/user_profiles.html` remain as unused files on disk (routes now redirect instead of rendering them). | PART J.4 |
| P3-6 | `migrations/env.py`'s own comments describe the production DB dialect as "MySQL," inconsistent with the actual enforced PostgreSQL-only gate. | PART E |

## SERVER_TEAM_VERIFY

- Live Razorpay webhook delivery to a real HTTPS staging endpoint (W1-W12 checklist, `docs/production/razorpay-certification.md`).
- Real-device/real-browser scanner certification for iOS Safari, Firefox, and the broader staging HTTPS device matrix — genuinely never performed (PART I.7). (Local verification already covers Chrome desktop, Edge desktop, Brave desktop, and Android Chrome, including over HTTPS for the scanner/camera flow — not an outstanding item.)
- Backup restore rehearsal actually executed against real infrastructure (procedure exists in-repo; execution against real infra is unconfirmable from the repo alone).
- First-production-bootstrap Alembic reconciliation: confirm whether a fresh production DB will boot via `db.create_all()` and then be explicitly `flask db stamp`-ed, per the tension identified in PART E.
- SMTP-in-anger (real provider, real volume, real failure modes) — no dedicated test file or incident procedure exists in-repo for this specifically (PART N).
- OS/Python version, Gunicorn worker count/timeout, reverse-proxy product/config, domain/DNS, CPU/RAM sizing — all genuinely undefined in this repository (PART L).

## BUSINESS_DECISION_REQUIRED

- Backup retention duration (repo explicitly defers this to business policy — PART O).
- `[Rollback Authority Role]` — a named role/person, left as an explicit placeholder in the repo's own runbooks (PART M/W).
- SLA/support-tier commitments — no SLA text exists anywhere in code or docs (PART S/V).
- All vendor/hosting pricing (PART V) — `TBD — COMMERCIAL INPUT REQUIRED` throughout, no prices invented.
- Staffing model for server/DevOps, app maintenance, and support/admin ops (PART V/W).
- Whether/when to finish or remove the dormant Experience Creator subsystem (flagged by the prior lineage audit as a standing "looks live but isn't" risk for future auditors — a product decision, not a code fix).

STATUS: every item above is either VERIFIED (with citation) or explicitly labeled `SERVER_TEAM_VERIFY`/`BUSINESS_DECISION_REQUIRED` — nothing in this register is manufactured.

---

# PART Y — Final Verdict

**RELEASE AUDIT: CONDITIONAL PASS**

Rationale against the stated PASS bar:
- **No P0/P1 product blocker** — TRUE for P0 (none found). The three P1 items (admin-nav consolidation, live webhook staging certification, real-device scanner certification) are none of them code defects — two are explicitly external verification work assigned to the server/staging team, and one (nav consolidation) is a UX-completeness item explicitly out of scope for this release under the "no redesign" instruction. None blocks the customer-paid workflow, which is fully closed and test-covered end to end.
- **Release contents clearly classified** — TRUE (PART Q: full A/B/C classification of all 69 entries, explicit staging manifest, no `git add .`/`-A` suggested).
- **No secrets intended for commit** — TRUE (verified: no `.env` in the dirty list; no secret values reproduced anywhere in this evidence document; `.env.example` contains only placeholders).
- **Scanner algorithm unchanged** — TRUE, verified with exact current-line citations of all 7 detection constants plus a full diff review proving zero scanner-logic touch in the release batch (PART I).
- **Production architecture understood** — TRUE (PART C, cross-verified against `docs/production/*.md`, an already-existing and current operations doc set).
- **Server-team dependencies documented** — TRUE (PART L/M, with every undefined item explicitly marked `SERVER_TEAM DECISION REQUIRED`, nothing invented).
- **No material undocumented deployment dependency remains** — TRUE, with two explicitly-flagged documentation-currency gaps (stale migration-chain runbook and stale `verify_alembic_state.ps1` head check, PART E/X) that must be fixed before those specific artifacts are relied upon — these are documentation drift, not missing dependencies.
- **All remaining external checks correctly assigned to staging/server/business** — TRUE (PART X's `SERVER_TEAM_VERIFY`/`BUSINESS_DECISION_REQUIRED` lists).

**Why CONDITIONAL rather than unconditional PASS**: this audit found genuine, honestly-disclosed external verification gaps that were open before this audit and remain open — live Razorpay webhook staging certification, and real-device/real-browser scanner certification. Per this project's own production-readiness documents, these are pre-existing, explicitly-named conditions for treating the release as production-ready, not new findings invented by this audit. The condition for an unconditional PASS is: complete the staging webhook certification (W1-W12) and the real-device scanner matrix before public production launch. Neither blocks a controlled staging deployment or further internal work.

---

*(End of evidence document body. This file was written incrementally across the audit, per instruction — see the STATUS tags throughout for what is VERIFIED vs. ASSUMPTION vs. TBD vs. DEFERRED_V1 vs. SERVER_TEAM_VERIFY.)*
