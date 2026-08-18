# V1.1 — Final Creator & Admin UI/UX Polish Report

Lane: `agent/v1.1-experience-ux` (Agent 2)
Worktree: `F:\ScanStory-main\ScanStory-v1.1-agent2`
Scope: presentation only. No backend rewrite, no business-rule change, no scanner change, no migration.

---

## 1. Starting HEAD

`da25e6483fbf814254b5ff5a524292155ce5741b`

Verified at lane start with `git -c safe.directory=... rev-parse HEAD`, `git branch --show-current`
(`agent/v1.1-experience-ux`) and `git status --short` (clean, no untracked files).

## 2. Synced integration HEAD

No sync performed this pass — none was required. The lane HEAD already equalled the authoritative
integration HEAD (`da25e648`) when work began, as stated in the task brief and re-verified above.
`main` was never merged and integration was never pulled.

## 3. Ending HEAD

`6a5f4f6473542ed1ddead40e5b80103ffd4a7dde`

## 4. Commits

| Hash | Subject |
|---|---|
| `7da3013` | Make Creator story state visible and repair feedback honest |
| `60cd663` | Unify the admin shell and speak operational language |
| `f2e6d48` | Give every screen a landmark, a skip link and named controls |
| `6a5f4f6` | Restore admin nav on two pages and certify the pass |

Four commits, split by concern rather than by file: one Creator-surface commit, one admin-console
commit, one cross-cutting accessibility commit, and one closing commit carrying the last hierarchy
fix plus this report and its evidence.

## 5. Files changed

39 files, ~940 insertions / ~375 deletions (excluding the report and screenshots).

**Creator templates:** `user/projects.html`, `user/user_create_project.html`, `user/dashboard.html`,
`user/profile.html`, `user/ownership.html`, `user/project_preview.html`, `user/landing.html`,
`user/subscribe.html`, `user/contact.html`, `user/login.html`, `user/register.html`,
`user/edit_project.html`, `user/forgot_password.html`, `user/reset_password.html`,
`user/verify_email.html`.

**Admin templates:** `admin/base.html`, `admin/operations.html`, `admin/moderation.html`,
`admin/ownership.html`, `admin/settings.html`, `admin/plans.html`, `admin/add_plan.html`,
`admin/edit_plan.html`, `admin/addons.html`, `admin/view_user.html`, `admin/users.html`,
`admin/projects.html`, `admin/scans.html`, `admin/payments.html`, `admin/view_payment.html`,
`admin/user_profiles.html`, `admin/user_scans.html`, `admin/manage_admins.html`,
`admin/activity_logs.html`, `admin/subscriptions.html`, `admin/login.html`.

**Tests:** `tests/integration/test_user_projects_page.py` (+4 focused state tests);
`tests/integration/test_admin_navigation_routing.py`, `tests/integration/test_v1_agent2_admin_parity.py`
and `tests/gate_jr/test_v11_commercial_ownership_ux.py` (6 assertions realigned to intentional
markup/copy changes — see §24).

**`app.py`:** three presentation-context passthroughs and nothing else — the readiness aggregates in
`projects_page()` (§25 D1) and `admin=current_admin()` on `admin_moderation_page` and
`admin_ownership_page` (§25 D8a). No route, query, permission check or business rule changed. No
other Python file was touched. `static/css/**` was not modified; every style change is scoped inside the
template that owns it, and the existing `.ss-skip-link` / `.ss-visually-hidden` tokens were reused
rather than re-invented.

---

## 6. Creator — Create ScanStory — result

Read `templates/user/user_create_project.html` first. The Details / Content / Review wizard, the
mobile step indicator, the sticky bottom CTA and the resumable-upload progress panel were all
already built by prior checkpoints and were **verified working, not rebuilt**.

Changed:

- **Story Name is now the headline of the Details step** — larger label, larger 17px input, heavier
  weight. It was one input among several; it is the only field every creator must fill.
- **Content requirements are stated before the file pickers**, not discovered through a rejected
  upload: accepted photo/video formats, what makes a good marker (detailed, high-contrast; avoid
  blank/blurry/reflective), and an explicit "background processing prepares your photo — your QR code
  is ready immediately and never changes" note.
- **"pair" is gone from Creator copy**, replaced by "content set" / "image + video set" throughout —
  the step help, the plan-limit line, the Add button (including its two JavaScript re-render paths),
  and the remove-control's accessible name.
- **Panel title changed from "Create ScanStory" to "Story details"**, so the desktop layout carries
  the same Details → Content → Review model the mobile wizard shows. The `<h1>` already said "Create
  ScanStory"; the panel was repeating it instead of naming the step.
- **Removing a content set now confirms** — but only when there is actually work to lose. Confirming
  an empty set would train people to click through the dialog.
- Both file inputs given accessible names (§13).
- Skip link and `<main>` landmark added.

**Verified unchanged and still correct:** only the three locked valid combinations are reachable.
`Direct QR` hides *and disables* the playback-mode radios, so no `playback_mode` is submitted at all
and the server applies its own default — the invalid combinations are unreachable from the form
rather than merely discouraged, with `_validate_project_experience_playback()` still the authority.
No experience/playback mapping was touched.

Screenshots: `evidence/v1_1_final_ui_ux/chrome-creator-create-1440.png`, `-390.png`.

## 7. Creator — My Stories — result

The largest single defect found in this pass. `/projects` offers a **status filter** with Ready /
Processing / Pending / Needs-fix options — and the cards never showed which of those states they
were in. The page could be filtered by a fact it refused to display.

The cause was not missing data. `projects_page()` already computed `ready_pair_count`,
`failed_pair_count` and `processing_pair_count` in the aggregate subquery that drives that very
filter, then selected only `pair_count` and threw the rest away. Those three columns are now
selected and attached to each project (§25 D1) — no extra query, no new rule, identical filter
semantics.

With that, each card now shows exactly one readiness state, always as **text plus** colour, never
colour alone:

| State | When | Card also says |
|---|---|---|
| Ready | every content set processed | — |
| Processing | any set in flight | "N of M content sets ready. This usually takes a minute." |
| Waiting to process | sets exist, none started | "Waiting for background processing to pick this up." |
| Processing failed | any set failed | "Some content sets could not be prepared. Use Try again below — your QR code will not change." |
| Needs attention | no content sets at all | "This story has no image + video sets yet." |
| Suspended | `is_active` false | existing suspension copy, unchanged |

**Suspension is checked first and never collapses into a processing or coverage state** — the locked
rule survives, and is now covered by a regression test (§24).

**Actions are contextual.** Previously every card showed View / QR / Edit / Fix / Delete regardless
of state. Now: **Test** appears only on a Ready story (testing an experience that is still
processing is a dead end); **Fix** never appears on a suspension (reprocessing cannot lift one, and
the card's own copy already says so); **Fix** is replaced by a non-clickable "Repair in progress"
while a run is active; on a failed story it becomes **Try again** in a warmer tone. View / QR / Edit
/ Delete remain always available.

Also fixed: page title said "My Story"; the count badge rendered "2 storys"; the empty-state CTA
sent new creators to `/dashboard` instead of `/create-project`; "pair" → "content set" on the meta
pill.

**Verified unchanged:** the `coverage_state` badges and their per-state explanatory copy (Agent 1's
P1-9 contract) branch only on the backend-resolved string and format a backend-provided date. No
date is compared in the template and "suspended" is still never rendered as "expired".

Screenshots: `chrome-creator-my-stories-1440.png`, `-390.png`.

## 8. Fix/Reprocess UX — result

**The contract that exists today.** `POST /projects/<id>/reprocess` marks every pair as processing,
schedules the job, flashes a message and **redirects**. It returns no job id and no JSON. A separate
`GET /api/processing/jobs/<job_id>` endpoint does exist (the create-project upload flow polls it),
but the repair route hands back nothing that could address it. The UI was therefore built strictly
against redirect-and-render — see §28 for the exact dependency gap.

What the action does now:

1. **Confirm** — "Repair this ScanStory? Your QR code will not change."
2. **Immediate visible response** — the button is disabled, gains `aria-busy`, and its label becomes
   a spinner plus **"Repairing…"**. Every other mutation button on the same card is disabled too.
   The action can no longer look inert between click and navigation.
3. **Duplicate prevention, two layers** — the form refuses a second submit client-side
   (`data-submitted`), and once the server reports the story as processing the control is *replaced*
   by a non-clickable **"Repair in progress"** rather than a button that would queue a second
   attempt. This is the "Repair is already in progress." state, expressed through the state the
   server already renders instead of an error message after the fact.
4. **Progress refresh** — while any story is in a processing state, a persistent (non-toast) banner
   says so, names how many stories are affected, and the page refreshes itself. Bounded to 20
   refreshes via `sessionStorage`, paused when the tab is hidden, and with an explicit **Stop
   auto-refresh** control, so a stuck job can never turn the page into an endless reload loop.
5. **Success** — the story reappears as **Ready**.
6. **Failure** — the story shows **Processing failed** with plain-language copy and a **Try again**
   control.

**Nothing internal is exposed.** A regression test asserts that `IntegrityError`, `idempotency`,
`RQ`, `Redis` and `processing_jobs` appear nowhere in the failed-state markup.

The refresh is deliberately the laziest thing that works against the real contract. When a
per-project status endpoint exists, exactly one call changes — the `window.location.reload()` inside
the watch becomes a fetch. Nothing else in the feedback layer depends on which one it is.

Screenshot: the "Repair in progress" state is visible on `chrome-creator-my-stories-1440.png` when a
story is mid-run; the disabled "Repairing…" transition is a client-side state on the same control.

## 9. Creator Dashboard — result

- **Broken copy repaired.** The readiness line rendered *"You're ready to create. You can still bring
  1 memories to life. remaining."* — a duplicated fragment plus an unguarded plural. It now reads
  "You're ready to create — 1 story left on your plan.", pluralises correctly, and says simply
  "You're ready to create." on an unlimited plan instead of printing a sentinel.
- **The hero heading said "Create Your First STORY" to everybody**, including creators with existing
  stories. It is now conditional.
- **Product language unified.** "Memory", "STORY", "story", "project" and "ScanStory" were all in
  use for the same object, often on the same screen. Every call to action is now **Create
  ScanStory**.
- **Two competing primaries removed.** The hero CTA and the Stories card both rendered a loud
  glowing "Create" button above the fold. The card now links to **View my stories** — a useful
  destination instead of a repeat.
- **The Quick Stats strip was deleted, not restyled.** Three of its four tiles restated the Plan /
  Stories / Scans cards directly above it, and the fourth was a raw visit counter with no defined
  meaning. Repeating a number does not make it clearer.
- **An internal release number was removed from customer-facing copy** — "Backend-sourced account
  limits and V1.1 experience availability" is now "What your account can do right now, including
  anything you have bought on top of your plan."
- "Pairs / Project" → "Content Sets Per Story", with a one-line explanation.
- Decorative blob overflow fixed (§18), skip link and `<main>` id added.

Not done, deliberately: no analytics, no charts, no new panels. The brief asked for less noise, not
more surface.

Screenshots: `chrome-creator-dashboard-1440.png`, `-390.png`.

## 10. Profile / Plan / Usage — result

Verified first: every entitlement number on `/profile` already comes from the backend resolver
(`entitlement_summary.*`) — base vs purchased vs admin-granted storage, effective project and scan
limits, per-file media limits, plan family, account type. **No business limit is computed in the
template.** The only arithmetic present is `used / limit * 100` for progress-bar widths, which is
presentation of two backend values, not a limit calculation. Nothing needed moving to the backend.

Changed:

- **The mandated over-limit sentence is now the first thing the over-storage block says**, verbatim:
  *"Your current storage use is above your plan allowance. Existing content is safe, but new uploads
  are paused."* The existing truthful detail (nothing is deleted; smaller replacements may be
  allowed) follows it. The same sentence was added to the dashboard's over-storage block.
- "Pairs / project" → "Content sets per story".
- Skip link, `<main>` landmark, root-level overflow clip.

Screenshot: `chrome-creator-profile-1440.png`.

## 11. Creator Ownership Center — result

Read `templates/user/ownership.html` in full before touching it. This template is the product of
four prior waves and was found **already correct** on every rule the brief lists:

- Incoming / outgoing / expired handovers are three separate sections. **Expired is listed apart
  from the two actionable lists on purpose** and carries no accept / retry / decline / withdraw
  control, because the backend offers none.
- Every status renders through `PROJECT_TRANSFER_STATUS_LABELS` / `PROJECT_CLAIM_STATUS_LABELS` — no
  raw enum reaches the page.
- **No copy implies ownership moved before completion.** Accepting "starts the final backend check";
  ownership "moves only after project and storage capacity pass"; a capacity block states "Ownership
  has not changed: the current owner remains current owner, and media plus QR remain intact"; an
  expired handover states "**Ownership did not move**" in bold and explains that a linked review
  request is a separate lifecycle.
- Deadlines are the backend's `expires_at`, formatted and nothing more.
- Claim eligibility lookup returns the server's own sentence, never a raw reason code.

The one gap: every mutation here is a POST + redirect and **no control showed it was working**. All
of them now disable, set `aria-busy` and show "Working…" after the confirmation is accepted. That is
the only change made to this file — verification found nothing else to fix, and inventing changes on
the highest-stakes Creator screen would have been the wrong instinct.

Screenshot: `chrome-creator-ownership-1440.png`.

## 12. Admin navigation — result

**The console had two different sidebars.** Roughly fourteen standalone pages included the shared
`admin/_sidebar_links.html` — grouped under Dashboard / User Management / Content / System, with
`aria-current`, `aria-hidden` icons, and an **Ownership Review** entry. The nine pages that extend
`admin/base.html` got a second, hand-maintained inline copy that was flat, ungrouped, and **had no
Ownership Review link at all** — so `/admin/ownership` rendered a sidebar with no entry for the page
you were standing on. Drift between two copies of the same nav is exactly how that happens.

Both shells now render the same partial. Consequences:

- Ownership Review is reachable from every admin page.
- Navigation is grouped everywhere, not just on half the console.
- **The sidebar is independently scrollable** (`max-height: 100vh; overflow-y: auto;
  overscroll-behavior: contain`). Its list is taller than a short viewport and the bottom entries
  were previously unreachable.
- **Mobile navigation exists.** The sidebar column was `d-none d-md-block`; below `md` the console
  previously had *no* navigation whatsoever — only an account dropdown. Below `md` the **same
  sidebar element** now becomes an off-canvas drawer, with a backdrop, `Escape` to close, focus
  moved into the drawer on open and back to the trigger on close.

  Worth recording how that landed, because the first attempt was wrong. The drawer initially worked
  by rendering `_sidebar_links.html` a *second* time inside the navbar collapse. It looked fine in
  the browser and it failed three tests in `test_admin_navigation_routing` — which assert that each
  admin destination appears exactly **once** in the document, a guard against the duplicate-nav bug
  a previous lane fixed. Duplicating the partial made every href appear twice. Rather than loosen a
  test that guards a real rule, the second render was removed and the one existing element is moved
  by CSS instead: one nav in the DOM, one set of links, no way for two copies to drift apart. The
  assertion stays literally true.

Also in the shell:

- **The account-menu trigger was `<a href="#">`** — the one dead link the prior certification found.
  It is a `<button>` now, with `aria-expanded` and a visually-hidden "Account menu" name.
- **Settings appeared in both the sidebar and the account menu.** The duplicate destination is gone;
  the account menu keeps only account actions (role, logout).
- The navbar's `navbar-light bg-white` classes — which the stylesheet then had to fight with
  `!important` — are gone, and the toggler gained `aria-controls` / `aria-expanded` / a label.
- Skip link added; `<main>` given `id="main-content"`.
- **Flash alerts no longer auto-dismiss indiscriminately.** All alerts vanished after five seconds.
  Errors and warnings now persist until dismissed and carry `role="alert"`; only confirmations
  auto-close. A problem report that disappears on a timer is a problem report nobody read.

No admin route was added, removed or renamed. This is link and shell cleanup only.

Screenshots: the unified grouped sidebar is visible on every `chrome-admin-*` screenshot.

## 13. Content Reports — result

Template confirmed as `templates/admin/moderation.html`. Verified already correct: labelled filter,
table caption, `scope="col"` headers, loading / empty / error rows, read-only state when the manage
permission is absent, an explicit suspension-consequences panel, and a confirmation before the one
transition that changes anything. **No moderation business rule was touched** — the modal still
offers exactly the three transitions the route accepts.

Added:

- **Report summary strip** — counts by status, computed from the same rows the table renders. No
  second query, no second source of truth. The page's job is triage; the triage numbers should not
  require scrolling a table to infer.
- **Project context and evidence in the review modal** — reported date, reporter (user / contact
  provided / anonymous), current status, and an **Open ScanStory** link to the admin project page.
  Previously the modal showed only name, reason and details.
- **Prior-decision history** — `reviewed_at`, reviewing admin, resolution action and note, all
  already present in `_content_report_payload()` and previously unused. Without it a reviewer cannot
  tell whether they are revisiting a decision or making the first one.
- **Save button busy state** — it was disabled on submit with unchanged text, which reads as a dead
  click. It now says "Saving…" and restores its label on both success and failure paths.

Screenshot: `chrome-admin-content-reports-1440.png`.

## 14. Admin Ownership Review — result

Treated as the highest-regression-risk template in the project. Read in full, and the following were
**verified still correct and left alone**:

- Terminal claims and transfers show "No available action" / "No available decision" and render no
  mutation control.
- Blocked admin actions surface `row.admin_block_reason` — the backend's own answer from
  `claim_admin_review_block_reason()`, resolved in the route. The condition is not restated in the
  template, so the page cannot drift from the function. Approve/reject would raise `PermissionError`
  in that state, so no control is offered rather than one that fails after the click.
- `EXPIRED`, `PENDING_CAPACITY` and `DISPUTED` remain three distinct states with three distinct
  sentences, and the expired note still states that a linked claim is a separate lifecycle.
- No transition logic, no route, no permission check was touched.

Changed:

- **Raw database enums were the visible badge text** — `PENDING_ACCEPTANCE`, `VENDOR_NOTIFIED`,
  `TRANSFER_COMPLETED`. The human label already existed underneath in muted grey. The label is now
  the badge; the raw code remains in `data-transfer-status` / `data-claim-status`, which is what
  tooling and tests read. `<code>EXPIRED</code>` in the intro prose became **Expired**.
- **Decision-oriented worklist summary** at the top: transfers awaiting a decision, review requests
  open, and how many are waiting on a managing vendor. This page's entire purpose is "what needs a
  decision now", which previously required reading two full tables.
- **The capacity block says what it means for the decision.** "Storage: blocked / Project slot:
  blocked / Project bytes: 4194304" became "Recipient storage: Not enough room / Recipient story
  slot: None free / This ScanStory needs: 4194304 bytes", plus an explicit "Completing this handover
  will not succeed until the recipient has room."
- All approve / reject / complete / dispute / cancel / release buttons now show a busy state. These
  are ownership-affecting actions on a full page round trip; an inert-looking button invites a
  second click.

One deliberate revert during this work: the intro prose was briefly reworded from "does not cancel a
linked claim" to "…linked review request" for internal consistency. That string is asserted by
`test_admin_ownership_page_distinguishes_expired_from_pending_and_disputed`. Rather than loosen a
test that guards a locked rule, the original wording was restored. The `EXPIRED` → **Expired**
product-language fix was kept.

Screenshots: `chrome-admin-ownership-review-1440.png`, `-390.png`.

## 15. Admin Operations — result

Verified already built and left intact: the refund attention worklist (rows from the backend's
single `stuck_refund_filter()` predicate), the manual-review note, the out-of-band provider-refund
table, refund eligibility and its disabled-with-reason control, and the labelled refund-reason
inputs.

Translated into operational language — every fact kept, every piece of infrastructure vocabulary
removed:

| Was | Is |
|---|---|
| "RQ / Worker Configuration" | "Background processing" |
| "Queue availability check: Reachable / Unavailable" | "Processing service: **Online** / **Unreachable** / **Not verified**" (badge + text) |
| "Mode: rq" / "Mode: fake" | "Processing runs: In the background / Immediately, in the web request" |
| "Redis configuration" | "Background storage" |
| "Queue: `<internal queue name>`" | **removed entirely** |
| "Pending / Running / Failed" | "Waiting / Processing / Needs attention" |
| "Safe error" | "Reported problem" |
| "SMTP" | "Email delivery" |
| "Host configuration" / "From configured" | "Mail server" / "Sender address set" |
| "the `/ready` probe (`checks.workers` / `usable_worker_count`)" | "the readiness probe" |
| "Queue ID" column (internal job handle) | **column removed** |
| raw `job.status` / `session.status` text | badges reading Waiting / Processing / Done / Needs attention, text always carrying the meaning |
| "Project" column headers | "ScanStory" |
| "Safe failure" | "Problem summary" |
| "Finished" | "Last attempt" |

No Redis URL, database URL, stack trace, raw SQL, token or internal queue implementation name is
rendered anywhere on the page — confirmed by reading the rendered HTML in-browser. Every table
gained `scope="col"` headers and a visually-hidden caption.

The three headings asserted by `test_v1_agent2_admin_parity` ("Operations Diagnostics", "Recent
Upload Sessions", "Recent Processing Jobs", "Current Entitlement Visibility") were kept — they are
already operational language, not implementation names.

Screenshots: `chrome-admin-operations-1440.png`, `-390.png`.

## 16. Admin Settings — result

The page was already honest — non-functional controls were genuinely `disabled` with explanatory
copy, not fake-editable. The defect was the *label*: every read-only group was badged **"Not active
in V1"**, an internal release number that tells an operator nothing about whether the control does
anything.

Every group now reads as exactly one of the two categories that actually apply here:

- **Editable** — Trial Settings. It has a real `POST` write path, so it is the only group labelled
  editable, and its Save button now shows "Saving…".
- **Read-only · managed by server** — General, Payment, Security. The explanatory copy was rewritten
  without version numbers and now says where the value actually comes from ("change the server
  configuration"), rather than describing what the app does not yet do.

A one-paragraph legend under the page title states the rule explicitly: *"Nothing on this page is
configurable in a way the app then ignores."*

**No control was made to look editable that is not.** Nothing was added.

The same internal release number was also removed from visible copy on `/admin/plans` ("V1.1
Commercial Policy Contract" → "Commercial Policy Contract"), `/admin/plans/add` and
`/admin/plans/<id>/edit` ("V1.1 plan policy status" → "Plan policy status"). Engineering rationale
in HTML comments was left alone — those are notes to maintainers, not UI.

Screenshot: `chrome-admin-settings-1440.png`.

## 17. Common UI states — result

| State | Result |
|---|---|
| Loading | Content Reports and Ownership use explicit loading rows; the resumable upload has a real progress panel. No spinner added where nothing is being waited for. |
| Empty | Distinct copy for "no results for this filter" vs "nothing here yet" on My Stories, Content Reports, Ownership, Operations tables. |
| Success | Flash messages plus, on My Stories, the persistent state badge — the outcome is visible after the toast is gone. |
| Warning / Error | Admin flashes with `role="alert"` now persist until dismissed; errors and warnings no longer auto-close. |
| Processing | New first-class state on My Stories with a per-card progress line and a persistent page-level banner. |
| Disabled | Disabled controls carry a reason (refund ineligibility, blocked admin claim review, read-only settings) rather than being silently inert. |
| Retry | "Try again" on a failed story; "Retry capacity check" on a blocked handover; "Recover / reconcile" on a refund. |
| Offline / network failure | Content Reports and the ownership claim lookup already had explicit network-error copy; verified and left. |
| Destructive confirmation | Present on delete, repair, plan lifecycle/delete, suspension, all ownership transitions, and now on removing a content set that holds files. |
| **No action appears dead after click** | Busy states added to: My Stories repair and delete, all admin ownership mutations, all Creator ownership mutations, the Content Reports save, and the Settings trial save. |

On the toast rule specifically: **no critical state is carried by a toast alone.** A repair's flash
message is transient, but the story's processing state, the progress line and the auto-refresh
banner are all rendered in the persistent UI.

## 18. Responsive matrix

Measured in **real Chrome** (`channel: 'chrome'`) with an Edge cross-check, against the seeded local
server, waiting for `networkidle` plus 600ms before measuring — the prior certification retracted six
findings that turned out to be mid-flight scroll-animation transforms, so every measurement here is
taken after settle and was confirmed identical across two consecutive runs.

Metric: `document.scrollingElement.scrollWidth` vs `clientWidth`, plus the bounding box of every
element whose right edge exceeds `clientWidth`.

| Route | 1440x900 | 1280x720 | 1024x768 | 768x1024 | 430x932 | 390x844 | 360x800 |
|---|---|---|---|---|---|---|---|
| `/admin/plans` | **0px** | **0px** | — | — | — | — | — |
| `/` (landing) | — | — | 0px | 0px | 0px | 0px | 0px |
| `/dashboard` | — | — | 0px | 0px | 0px | 0px | 0px |

Both previously-reported MEDIUM responsive defects are **closed and re-measured**:

- **`/admin/plans`** was `scrollWidth=1534` vs `clientWidth=1440` (+94px), with the lifecycle **Set**
  form and the **Delete** button at `x=1479..1534` — entirely outside the viewport. Root cause: a
  nowrap flex row inside a 360px plan card, with nothing in the ancestor chain
  (`.plan-actions` → `.plan-card` → `.plans-grid` → `.main-content` → `body`) clipping or scrolling.
  Fixed by letting the row wrap and stopping the lifecycle `<select>` forcing the line wide. Now
  **0px, 0 offending elements** at both 1440 and 1280, on Chrome and Edge.
- **Decorative blobs** caused 12–30px of drag-sideways on `/` and `/dashboard`. `body { overflow-x:
  hidden }` never contained them because they are `position: fixed` — their containing block is the
  viewport, so no ancestor's overflow can clip them. Clipping moved to the root element, using
  `overflow-x: clip` rather than `hidden` so `position: sticky` inside those pages keeps working.
  Now **0px page overflow at every tested viewport**, on Chrome and Edge, while the blobs still
  overhang their boxes (verified: `#blob2` right edge 409.5 vs clientWidth 390 — overhanging and
  correctly clipped, which is the intended decorative behaviour).

The landing page's `.who-buttons-container` still measures ~1272px wide inside a 390px viewport, but
it sits inside `.who-buttons-wrapper` (`overflow-x: auto`) inside a section with `overflow-x:
hidden` — an intentional horizontal scroller, not a leak. Recorded as expected behaviour, not a
defect.

Every route in §22 was additionally rendered at 1440x900 and the six most action-dense surfaces at
390x844 (see `evidence/`): no clipped content, no offscreen action, no nav collision, no fixed
element covering content, no unreadable badge.

## 19. Accessibility — result

Scripted DOM audit across **22 routes**, three separate browser contexts (anonymous / creator /
admin — a shared cookie jar would silently redirect `/login` and `/register` and skip their audits).

| Criterion | Before this pass | After |
|---|---|---|
| Document title present | 22/22 | **22/22** |
| Exactly one `<h1>` | 21/22 (`project_preview` had none) | **22/22** |
| `<main>` landmark | 10/22 | **22/22** |
| Skip-to-content link | **0/22** | **22/22** |
| Visible controls with no accessible name | **25** across 4 routes | **0** |

All three previously-reported MEDIUM accessibility findings are closed:

- **No `<h1>` on `project_preview`.** The project's name was already on the page — it just was not a
  heading. It is now the `<h1>`, and the line above it reads "Viewing ScanStory N".
- **25 unlabeled visible controls.** The worst were on `/admin/users/<id>`, where three separate
  entitlement-mutation forms each carried a bare "Reason" textbox — a screen reader announced three
  identical unnamed fields with no way to tell which action each belonged to. Also `/admin/addons`
  (ten catalog fields whose `<label>` elements wrapped nothing and carried no `for`, eight of them
  without even a placeholder), `/admin/plans` (the lifecycle select), `/contact` (six
  placeholder-only fields), and `/create-project` (both file inputs). Every one now has an
  accessible name.
- **No `<main>` landmark on 12 routes and no skip link anywhere** — despite the design system
  already shipping an unused `.ss-skip-link`. Where a page's own wrapper could not become `<main>`
  without disturbing its layout, `role="main"` produces the same landmark with a zero-risk diff.

One placement bug caught by re-measurement rather than by reading: on `/subscribe` the landmark
initially landed on a `.main-content` div nested inside the flash-message conditional, so it existed
only when there was a flash to show. Moved to the real content wrapper and re-verified.

Also addressed: decorative icons given `aria-hidden` on every touched control, table captions and
`scope="col"` added to Operations tables, `aria-busy` on every new busy state, `role="status"` on
the processing banner and `role="alert"` on persistent admin errors.

Not re-tested this pass (verified PASS by the prior certification, and nothing in this diff changes
focus behaviour): visible focus indicator on every tab stop, Tab order, keyboard-usable modals, and
"no hover-only critical action".

## 20. Chrome result

Real Chrome, `channel: 'chrome'`, `C:\Program Files\Google\Chrome\Application\chrome.exe`.

22 routes: **all HTTP 200**. Zero uncaught JavaScript errors. Zero console errors on any
first-party page. Zero static 404s. Zero CSP violations from first-party code. Zero mixed content.
No layout thrash observed on any page.

## 21. Edge result

Real Edge, `channel: 'msedge'`, `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`.

Identical results to Chrome on all 22 routes — same statuses, same zero first-party console/network
errors, same 0px overflow on `/admin/plans` (1440 and 1280) and on `/` (390x844), same accessibility
audit outcome (no `main=NO`, no `h1=0`, no unlabeled controls).

**Firefox: not installed** on this machine (`C:\Program Files\Mozilla Firefox\firefox.exe` absent) —
confirmed, not assumed. **Safari: unavailable on win32.** Neither was faked or estimated. This
matches the prior certification's finding.

## 22. Console / network result

| Route | Console errors | Page errors | Failed requests | HTTP ≥400 | CSP |
|---|---|---|---|---|---|
| `/` | 0 | 0 | 2 (see T2) | 0 | 0 |
| `/login/`, `/register`, `/contact` | 0 | 0 | 0 | 0 | 0 |
| `/dashboard`, `/projects`, `/create-project`, `/ownership` | 0 | 0 | 0 | 0 | 0 |
| `/profile`, `/subscribe`, `/project/1/preview` | 1 (see T1) | 0 | 2 (T1) | 0 | 1 (T1) |
| all 10 `/admin/*` routes | 0 | 0 | 0 | 0 | 0 |

Every non-zero cell is third-party and pre-existing:

- **T1 (LOW, third-party, unchanged):** `cdn.razorpay.com/static/cx/razorpay-risk-detection/bundle.js`
  is CSP-blocked on the three Razorpay-checkout pages. The allowlist covers `checkout.razorpay.com`
  but not `cdn.razorpay.com`, which checkout pulls into the top-level document. Checkout itself
  initialises normally; this degrades Razorpay's fraud telemetry, not payment capability. A related
  `checkout-static-next.razorpay.com/build/undefined` request is blocked by ORB — a malformed URL
  built inside Razorpay's own bundle, not by this codebase. Fixing either needs an `app.py` CSP
  change → **reported, not fixed** (§28).
- **T2 (LOW, unchanged):** `/media/demo` and `/media/art` report `net::ERR_ABORTED` on the landing
  page — media elements aborting their own fetches, with no HTTP error status. Benign negotiation.

The scanner CSP/OpenCV release blocker was **not investigated** — explicitly Agent 1's, explicitly
out of scope (§28).

## 23. Screenshots / evidence

27 Chrome screenshots committed to `evidence/v1_1_final_ui_ux/` (Edge equivalents captured and
compared, not committed — they are pixel-equivalent and would double the artefact size for no added
signal).

Desktop (1440x900): landing, login, register, contact, creator dashboard, My Stories, create,
ownership, profile, plans, project preview, admin dashboard, admin users, admin view-user, admin
projects, admin plans, admin add-ons, admin ownership review, admin content reports, admin
operations, admin settings.

Mobile (390x844): creator create, My Stories, dashboard, admin operations, admin ownership review,
admin plans.

**All synthetic test data.** The seeded fixture contains one creator (`cert.creator@example.test`)
and one admin on a throwaway SQLite database in a scratchpad directory. No real user data, no
credential, no token and no provider payload appears in any capture.

## 24. Focused tests

Per the brief, the full suite was **not** re-run repeatedly. Targeted runs during the work, then one
broader focused batch at the end.

**New tests** (4, in `tests/integration/test_user_projects_page.py`) covering the state contract this
pass introduced:

- `test_projects_card_shows_ready_state_and_test_action`
- `test_projects_card_shows_failed_state_with_try_again` — also asserts that `IntegrityError`,
  `idempotency`, `RQ`, `Redis` and `processing_jobs` appear nowhere in the markup
- `test_projects_card_in_progress_hides_duplicate_repair_control` — asserts the repair form is
  *absent* while a run is in flight
- `test_projects_card_suspended_is_distinct_and_offers_no_repair` — guards the locked rule that a
  suspension is not a coverage state and is not repairable

**Runs:**

| Batch | Result |
|---|---|
| `test_user_projects_page.py` (during Creator work) | 9 passed |
| `test_v11_final_ui_completion.py` (during admin ownership work) | 35 passed, 1 failed → root-caused and fixed → 36 passed |
| `test_user_projects_page.py` + `test_v1_agent2_admin_parity.py` | 58 passed |
| `test_admin_navigation_routing.py` (after the sidebar unification) | 3 failed → root-caused, approach changed, 2 assertions realigned → **29 passed** |
| Final focused batch, first run | 219 passed, 4 failed → all four realigned → **4 passed** |
| Final focused batch, confirmation run | **223 passed, 0 failed** (14m 47s) |

**Six test assertions were realigned**, all of them naming markup or copy this pass changed on
purpose. Each realignment preserves — and in three cases strengthens — what the test actually
guards:

| Test | Was asserting | Now asserts |
|---|---|---|
| `test_admin_nav_exposes_..._once` | `<i class="fas fa-users"></i> Users` appears once | `<span>Users</span>` appears once (the shared partial's markup) |
| `test_admin_nav_shows_capacity_link_with_active_state...` | the removed inline sidebar's exact `<a class="nav-link active" aria-current="page" href=...>` string | `href="/admin/capacity" class="sidebar-link active"` **and** `aria-current="page"` |
| `test_admin_settings_dead_fields_are_disabled` | `"Not active in V1"` present | `"Read-only"` + `"managed by server"` present **and** `"Not active in V1"` absent |
| `test_operations_page_distinguishes_configured_from_healthy` | `"Queue availability check"`, `"does not prove a worker is online"` | `"Processing service"`, `"not proof that a worker is actually running"`, **plus a new negative guard** that `Redis`, `RQ /`, `SMTP`, `Queue ID` and `usable_worker_count` do not appear |
| `test_admin_plan_pages_expose_policy_contract...` | `"V1.1" in body` — literally asserting the version leak the brief requires removing | `"policy"` present **and** `"V1.1"` absent |
| `test_moderation_permission_codes_are_the_real_ones` | `admin_can('admin.reports.view')` in `admin/base.html` | the same gate in `admin/_sidebar_links.html`, **plus** that `base.html` includes that partial |

**One test was deliberately NOT changed.** `test_admin_ownership_page_distinguishes_expired_from_pending_and_disputed`
asserts the phrase "does not cancel a linked claim". A wording tweak for internal consistency broke
it; since that string guards a locked ownership rule, the wording was reverted rather than the test
loosened.

Final batch files: `test_v11_final_ui_completion.py`, `test_v1_agent2_admin_parity.py`,
`test_user_projects_page.py`, `test_admin_navigation_routing.py`,
`tests/security/test_csrf_and_headers.py`, `tests/security/test_admin_panel_repair_csrf.py`,
`tests/gate_jr/test_v11_commercial_ownership_ux.py`, `tests/gate_jr/test_v11_admin_refund_ux.py`.

The two security files were included because this pass modified forms (busy-state handlers on
ownership, repair, delete and settings submissions) — CSRF tokens and header behaviour had to be
confirmed unaffected.

PostgreSQL migration certification was **not** run — no migration exists in this lane (§29).

## 25. Exact defects fixed

**Functional / correctness**

| # | Defect |
|---|---|
| D1 | `projects_page()` computed three readiness aggregates to drive the status filter, then discarded them — so `/projects` could be *filtered* by a state it could never *display*. Now passed through. |
| D2 | The Fix/Reprocess action gave no visible response between click and page reload, and could be submitted twice. |
| D3 | Every My Stories card offered every action regardless of state, including "Test" on a story that was still processing and "Fix" on a suspension that reprocessing cannot lift. |
| D4 | `admin/base.html` carried a divergent second copy of the admin sidebar with **no Ownership Review link**, so `/admin/ownership` had no nav entry for itself. |
| D5 | The admin console had **no navigation at all** below the `md` breakpoint. |
| D6 | The admin sidebar was not independently scrollable; on a short viewport its bottom entries were unreachable. |
| D7 | All admin flash alerts auto-dismissed after 5s, including errors and warnings. |
| D8 | `/subscribe`'s main landmark was nested inside the flash-message conditional (caught by re-measurement after the first fix attempt). |
| D8a | **`/admin/moderation` and `/admin/ownership` rendered with no sidebar, no account menu and no logout at all.** Both extend `admin/base.html`, whose entire shell sits inside `{% if admin %}`, and neither route passed `admin` to `render_template`. Found only by driving a real browser — every static read of the templates looks correct. Fixed with a presentation-context passthrough (§25 D1 rationale; `current_admin()` is already resolved on those requests by the permission decorator and the `admin_can` context processor). |
| D8b | The ownership worklist summary badge used `bg-light text-dark`, but the admin stylesheet redefines `.text-dark` to the light console text colour — light text on a light pill, invisible. Caught by looking at the screenshot, not by any automated check. |

**Responsive**

| # | Defect |
|---|---|
| D9 | `/admin/plans` +94px horizontal overflow at 1440x900, with the lifecycle **Set** form and **Delete** button fully offscreen. |
| D10 | `position: fixed` decorative blobs produced 12–30px of unintended horizontal page scroll on `/` and `/dashboard` at five viewports; `body { overflow-x: hidden }` could not clip them. |

**Accessibility**

| # | Defect |
|---|---|
| D11 | 25 visible form controls with no accessible name, across `/admin/users/<id>`, `/admin/addons`, `/admin/plans`, `/contact` and `/create-project`. |
| D12 | `project_preview` had no `<h1>`. |
| D13 | No `<main>` landmark on 12 routes; no skip link on any route. |
| D14 | `<a href="#">` used as the admin account-menu trigger (dead link). |
| D15 | Duplicate `/admin/settings` nav destination (sidebar + account menu). |

**Copy / product language**

| # | Defect |
|---|---|
| D16 | Dashboard: *"You can still bring 1 memories to life. remaining."* — duplicated fragment, unguarded plural. |
| D17 | Dashboard heading said "Create Your First STORY" to creators who already had stories. |
| D18 | My Stories rendered "2 storys"; page title read "My Story". |
| D19 | Internal release number visible in Creator copy (dashboard entitlement panel) and in four admin screens. |
| D20 | Operations exposed the queue backend name, its storage configuration, the internal queue name, the internal job handle, the mail-protocol name, and probe field paths. |
| D21 | Admin Ownership Review displayed raw database enums (`PENDING_ACCEPTANCE`, `VENDOR_NOTIFIED`, …) as badge text. |
| D22 | Admin Settings badged non-functional groups with an internal release number instead of a capability category. |
| D23 | "pair" / "pairs per project" used throughout Creator and admin copy instead of "content set" / "image + video set". |
| D24 | My Stories empty-state CTA sent brand-new creators to `/dashboard` rather than `/create-project`. |
| D25 | Dashboard had two competing loud primary CTAs above the fold. |

## 26. Remaining MEDIUM findings

**None.** All five MEDIUM findings carried in from the prior certification (M1 `/admin/plans`
overflow, M2 blob overflow, M3 missing `h1`, M4 unlabeled controls, M5 missing landmarks/skip links)
are closed and re-measured in real Chrome and Edge. No new MEDIUM finding was introduced or
discovered.

## 27. Remaining LOW / POLISH findings

| # | Finding | Why not fixed here |
|---|---|---|
| L1 | `cdn.razorpay.com` risk-detection bundle CSP-blocked on `/profile`, `/subscribe`, project preview. Degrades Razorpay fraud telemetry, not payment capability. | Needs an `app.py` CSP allowlist change → §28 |
| L2 | `checkout-static-next.razorpay.com/build/undefined` blocked by ORB — a malformed URL constructed inside Razorpay's own bundle. | Third-party; not ours to fix |
| L3 | `/media/demo` and `/media/art` `ERR_ABORTED` on landing — media elements aborting their own fetches, no HTTP error. | Benign; no user-visible impact |
| L4 | `COMPLETED` / `CANCELLED` transfers absent from the user `/ownership` lists. | **By design** per the route's own comment (the lists deliberately mean "still actionable"); visible via the claim record and the admin console. Changing it needs an `app.py` query change |
| L5 | No user-facing payment-history route or template exists. | Product gap, not a regression. Recorded |
| L6 | `admin/activity_logs.html` and `admin/subscriptions.html` got a skip link but no landmark — neither has a single wrapping content container to tag without restructuring. | Both are outside the 22 audited critical routes. Recorded as **not addressed this pass** |
| L7 | Admin Operations shows connection security as the raw lowercase transport value (`starttls`). | Cosmetic; the value is a real configuration string, not internal jargon |
| L8 | The audit harness reports `/admin/view-user/<id>` as 404 — that route does not exist; the real one is `/admin/users/<id>`. | Not a defect. The brief's route list used a stale path; recorded so the next lane's harness does not chase it |

**Not addressed this pass** (stated rather than silently skipped): §14 visual polish was applied only
where it followed from a function or clarity fix — badge and button hierarchy on My Stories,
Operations and Ownership Review, card density on the dashboard, spacing on the new create-page
requirements block. No standalone visual-polish sweep of untouched screens (typography scale audit,
shadow/border normalisation across all 60 templates, motion design) was performed. Priority order in
the brief put it last, and function, clarity, responsiveness and accessibility consumed the pass.

## 28. Agent 1 dependencies

**1 — Fix/Reprocess status contract (blocks a genuine improvement, not a shipping blocker).**

`POST /projects/<id>/reprocess` currently sets pair status, schedules the job, flashes and
**redirects**. It returns no job identifier and no JSON. `GET /api/processing/jobs/<job_id>` exists
and is already polled by the create-project upload flow, but the repair route hands back nothing
that could address it.

The UI was therefore built strictly against what exists: a bounded page refresh driven by the
server-rendered state, with an explicit stop control. It is honest and it works. **No endpoint shape
was guessed at.**

What would let the UI poll properly, in Agent 1's gift and in rough order of value:

- Have the repair route return (or flash-carry, or expose on the project row) the scheduled job id,
  so the existing `/api/processing/jobs/<id>` poll can be reused verbatim. This is the smallest
  change and needs no new endpoint.
- Or expose per-project processing state as JSON, so the card can refresh itself without a full page
  load.
- If the "already running" case is to be reported by the backend rather than inferred from rendered
  state, a distinct, safe, plain-language response is needed. Today the UI prevents the second
  attempt client-side and, once the server reports processing, replaces the control entirely — so
  the case is covered, but by inference rather than by contract.

When any of these lands, exactly one call in `templates/user/projects.html` changes. Nothing else in
the feedback layer depends on which.

**2 — CSP / OpenCV release blocker: explicitly NOT touched.**

Production CSP `script-src` lacks `'unsafe-eval'`, and `static/js/opencv.js` needs `new Function`,
breaking `tracked_overlay` and `detect_once` under enforced CSP. This is Agent 1's security-policy
decision. It was **not investigated, not measured and not worked around** in this lane, per the
brief. The scanner runtime files are byte-identical (§30).

**3 — Razorpay CDN allowlist (LOW).** Adding `cdn.razorpay.com` to `script-src` would clear L1. It
is an `app.py` CSP change and therefore not this lane's.

**4 — Completed/cancelled transfer history on `/ownership` (LOW).** Would need the route query in
`app.py` widened. Recorded as by-design; raised only so the decision is explicit.

## 29. Migration status

**No migration exists in this lane.** `git status --short migrations/` is empty across all four
commits; the directory is untouched. `models.py` was not modified. PostgreSQL migration certification
was correctly not run.

## 30. Scanner hashes before / after

| File | Before | After | Match |
|---|---|---|---|
| `scanner_runtime.py` | `a092b3f141f4e1ca743e45693db5b3560843b86baf59b853570607174982af16` | `a092b3f141f4e1ca743e45693db5b3560843b86baf59b853570607174982af16` | **byte-identical** |
| `static/js/scanner-runtime.js` | `95d5305dd3f8c1c0d1db84ca90b51fe79b8bb322bf1b1a2a3e771c270b3eb7b3` | `95d5305dd3f8c1c0d1db84ca90b51fe79b8bb322bf1b1a2a3e771c270b3eb7b3` | **byte-identical** |

Recorded before any edit and re-checked after the final commit. `templates/user/scanner.html` was
also not modified. No scanner recognition, tracking or runtime code was touched.

## 31. `git diff --check`

Clean — no whitespace errors, no conflict markers.

The only output is git's routine `LF will be replaced by CRLF` normalisation notices on templates
whose line endings were normalised by the editing pass. These are informational (`core.autocrlf` is
`true`); the repository content is unaffected, and `git diff --stat` confirms no line-ending
explosion — 39 files, ~940 insertions / ~375 deletions of real content.

## 32. `git status --short`

```
(empty - working tree clean, nothing untracked)
```

Verified after the final commit. The four commits are the entire change set; no
scratchpad artefact, harness script or temporary file was left in the worktree.

## 33. Staging recommendation

**Recommend staging this lane.**

Rationale:

- Every change is presentation-layer. The single `app.py` edit selects three aggregate columns that
  the same query already computed and attaches them to the template context — no new query, no
  changed filter semantics, no business rule, no new route.
- All five inherited MEDIUM findings are closed and independently re-measured in two real browsers.
  No MEDIUM remains.
- Zero uncaught JavaScript errors, zero first-party CSP violations, zero static 404s, zero mixed
  content across 22 routes on Chrome and Edge.
- Scanner runtime files are byte-identical; the absolute backend boundary was not crossed.
- No migration.

Two conditions on the staging decision, neither owned by this lane:

1. **The scanner CSP/OpenCV release blocker is still open and still a NO-GO for production.** This
   lane did not touch it and does not claim it. Staging this UI work is safe and independent, but
   the blocker must be resolved by Agent 1 before production release.
2. The Fix/Reprocess feedback layer ships against the redirect contract. It is correct and complete
   as-is; it should be revisited (one line) when Agent 1's status contract lands, so the repair state
   updates without a page reload.

Suggested smoke checks after staging: `/projects` with one story mid-processing (state badge,
banner, disabled repair control), `/admin/ownership` on a short viewport (sidebar scroll), and
`/admin/plans` at 1440x900 (Delete button on-screen).
