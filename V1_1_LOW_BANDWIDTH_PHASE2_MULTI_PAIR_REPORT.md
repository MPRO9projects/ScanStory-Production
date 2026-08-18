# V1.1 Low-Bandwidth Phase 2 — Multi-Content-Set Resumable Upload

Phase 1 shipped a genuinely good single-pair resumable uploader and then named,
in its own limitations list, the hole it left: *"Multi-pair projects still use
the legacy non-resumable uploader. Every improvement here applies to the
single-pair path only. A multi-pair upload on a weak link is still
all-or-nothing. This is the largest remaining gap."* It also flagged a second,
quieter one: a paused upload's real lifetime was bounded by a cleanup window of
two hours, not the twenty-four the UI implied.

This pass closes both. The principle it was built against, and the sentence
every test in it defends: **every byte the server has confirmed stays confirmed
— for every content set, even when a different content set fails.** A creator
with three content sets must never re-upload set 1 because set 3 broke.

---

## 1. Starting HEAD

```
29eb9faed96810016a6bcf22dfa0ae4e2704577d
```

Branch `agent/v1.1-platform-admin`, verified clean and synced to the
authoritative integration HEAD before any work began (`git status`, `git
branch --show-current`, `git rev-parse HEAD`).

## 2. Ending HEAD

```
64fd97530a780a2a1a6e972277adae4421d2e53f
```

(`66393e1` is the last code/doc commit; `64fd975` adds this report.)

## 3. Commits

| Hash | Subject |
|---|---|
| `9e04862` | Converge multi-content-set projects onto the resumable uploader |
| `8d7c22d` | Stop reaping a paused upload after two hours |
| `05aaee7` | Upload every content set resumably, and say so honestly |
| `1b8b50b` | Cover the 25 multi-content-set scenarios, re-point two shape guards |
| `66393e1` | Measure the larger-file claims, document the new contract |
| `64fd975` | Report the multi-content-set upload pass |

Split so each is independently revertable. The long-pause default is its own
commit precisely because it is a policy knob an operator may want to reason
about separately from the architecture.

## 4. Files changed

```
 app.py                                              | 859 +++++++++++-----
 docs/development/phase2_multi_pair_upload_measurements.json | 333 ++++++
 docs/development/resumable-upload-api-contract.md   | 155 +++-
 models.py                                           |  13 +-
 scripts/dev/multi_pair_upload_measurement.py        | 332 ++++++
 templates/user/user_create_project.html             | 463 +++++++-
 tests/gate_jr/test_marker_selection_upload.py       |  53 +-
 tests/integration/test_multi_pair_resumable_upload.py | 995 ++++++++++++++++
 8 files changed, 2937 insertions(+), 266 deletions(-)
```

Plus `V1_1_LOW_BANDWIDTH_PHASE2_MULTI_PAIR_REPORT.md` (this file), for nine in
total across the range.

`app.py`'s large number is mostly the one generalised finalize function being
rewritten in place rather than new surface: the net new server code is the
group-finalize route, two small payload helpers, the purpose guard and the
liveness touch.

## 5. Old multi-pair architecture (as audited, before anything was written)

Read in full first. The audit is what chose the design, and one of its findings
ruled out the approach that looked obvious.

**Legacy multi-pair path — `handle_upload()`, `app.py` line 8460.** One
`POST /upload`, `multipart/form-data`, carrying every marker and every video in
a single request body. `request.files.getlist("images")` /
`getlist("videos")`, paired by index. Then, in strict order: validate **every**
file from its actual content; precheck account storage over the whole retained
set; open a transaction; `reserve_account_storage` for the summed bytes;
`_reserve_project_quota_atomic(user)` **once**; create the `Project`;
`_reserve_pair_slots_for_project(project.id, pair_count, max_pairs)`; loop
`os.replace()`-ing each validated temp into place and creating each
`ProjectPair` at `pair_index = i`; `record_pair_media_objects` per pair; commit;
generate one QR; `_schedule_project_pair_processing(project.id)` **once**.

The comment in that function states the intent plainly and it matters: *"The
ENTIRE retained logical set is weighed at once — a multi-pair project is
accepted or rejected whole, so we never persist pair 1 and then reject pair 2
leaving a half-created project with orphaned accounting."*

**So: project creation timing was already all-or-nothing, at the END.** No draft
row, no partial project, ever. This is the single most important audit finding,
because it fixed the atomicity design (§7) before any code was written.

**Resumable path (Phase 1) — `UploadSession`, `models.py` line 2211.** One
session = one image + one video as a single sequential byte stream split at
`image_size`; `current_offset`, `expected_total_size`, `status`,
`storage_token` (server UUID4), inactivity `expires_at`, optional
`client_checksum_sha256`. Two DB CHECK constraints already bound the offset.
`project_id` / `pair_id` are outputs, set at finalize, `ondelete="SET NULL"`.
Routes: create / chunk / status / finalize / cancel, plus the
`cleanup-upload-sessions` CLI. The model docstring said it outright: *"Multi-pair
resumable projects and 'attach a resumable pair to an existing project' are out
of scope for this wave."*

**Client — `templates/user/user_create_project.html`.** The submit handler
branched on `readyPairs.length === 1`: one pair went to
`submitResumableSinglePair()`; anything else fell through to
`setUploadProgress('Preparing upload', 5, 'Using the legacy uploader for
multi-pair projects.')` and a plain `XMLHttpRequest` multipart POST. Phase 1's
localStorage record (`scanstory.resumableUpload.v2`) held exactly one session.

**What the audit ruled out.** "One session per content set" cannot work on its
own, because `_finalize_assemble_and_validate()` *creates a new Project*. Three
sessions finalizing independently would produce three projects, three quota
units and three processing jobs. And a parent-draft-project row (the other
obvious option) would break the existing all-or-nothing rule, put a 0-pair
project into a projects list whose status filters already assume
`pair_count > 0`, and charge quota for content that had not finalized. Neither
option survived the audit; the design in §6 came from what the code actually
does.

Also confirmed during the audit: `enqueue_project_pair_processing(project_id)`
is per **project** and dedupes against an active job (it returns
`created=False` and logs `processing_job_duplicate_ignored`), so one enqueue
covers all pairs — which is what makes §19 achievable without new machinery.
`direct_qr` correctly requires `image_size == 0` and produces
`is_processed=True`, `feature_extraction_status="not_required"`, no enqueue.

## 6. New multi-pair architecture

One `UploadSession` per content set — unchanged chunk contract, unchanged
offset semantics, unchanged retry rules — plus **one atomic project finalize
across N of them**.

```
set 1 ──► POST /api/uploads/sessions  (purpose: project_content_set)
          POST .../<id>/chunk  ×N  ───────────────┐
set 2 ──► POST /api/uploads/sessions             │
          POST .../<id>/chunk  ×N  ───────────────┤
set 3 ──► POST /api/uploads/sessions             │
          POST .../<id>/chunk  ×N  ───────────────┤
                                                  ▼
                    POST /api/uploads/projects/finalize
                          {"session_ids": [s1, s2, s3]}
                                                  │
              ONE conditional UPDATE claiming exactly N rows
                                                  ▼
       1 Project · 3 ProjectPairs (pair_index 0,1,2) · 1 quota unit
       · 6 MediaObject rows · 1 QR · 1 processing job
```

Three decisions, each made because of something in §5 rather than by
preference:

**1. `_finalize_assemble_and_validate()` was generalised, not duplicated.** Its
signature went from `(session_row, user, admin)` to `(session_rows, user,
admin)` — an ordered list — and the single-pair route now calls it with
`[session_row]`. Length changes none of the invariants. Keeping it one function
is the whole point: a parallel multi-pair finalizer would have to be kept in
step with the single-pair one forever, and quota, storage, ledger and QR
handling are exactly the parts nobody notices drifting.

**2. The group is defined by the request, not by a row.** No parent table, no
group-id column, no draft project — and therefore **no migration** (§27). The
`session_ids` array in the finalize body *is* the group. Every guarantee comes
from widening the conditional UPDATE Phase 1 already used:

```sql
UPDATE upload_sessions SET status='finalizing'
 WHERE id IN (...) AND status='active'
   AND current_offset = expected_total_size
```

If that does not move exactly N rows, nobody finalizes and nothing is created.
That single statement is what makes a double-clicked Create, a request retried
after a lost response, and two racing tabs all resolve to one project.

**3. A content set is marked as one at creation.** `purpose` gained the value
`"project_content_set"` (§27 — a new allowed value in an existing `String(30)`
column with no CHECK constraint on it, so not a schema change). The
single-session finalize route refuses those with `409
GROUP_FINALIZE_REQUIRED`. This closes a hazard the tests found rather than
assumed: without it, a client bug could finalize content set 2 of 3 on its own,
producing a stray one-pair project and burning a project-quota unit on it.

**Client.** `submitResumableMultiPair()` reuses `uploadResumableStream()`,
`createResumableSession()`, `sequentialUploadSlice()`, `fileFingerprint()`,
`fingerprintsMatch()`, `storedSessionMatchesFiles()`, `getUploadSessionStatus()`
and the whole retry/pause policy. It adds ordering, per-set bookkeeping and the
group finalize — nothing else. The `readyPairs.length === 1` branch still calls
`submitResumableSinglePair()` unchanged, because a one-set project needs no
group finalize and no per-set bookkeeping.

The legacy multipart XHR is now reachable **only** behind
`resumableUploadSupported()` (`fetch` + `AbortController` + `Blob.prototype.slice`).
It was kept rather than deleted for one honest reason: it is a real fallback for
a browser that cannot run the resumable path, and its progress/error UI carries
real test coverage. Its copy no longer claims to be the multi-pair uploader.

## 7. Project atomicity semantics

**Preserved exactly, because the audit found the existing model was already
right.** The Project row is created *after* every content set is byte-complete
**and** every set has validated — the same all-or-nothing guarantee
`handle_upload()` has always had for an N-pair multipart POST. There is no
draft project, no 0-pair project, no new workflow engine.

Concretely, in one transaction: storage reserved for the summed bytes → one
`_reserve_project_quota_atomic()` → `Project` created → pair slots reserved for
N → N `ProjectPair` rows → 2N `MediaObject` ledger rows → commit. Then QR, then
one enqueue.

Against the list of things to avoid:

| Risk | Why it cannot happen |
|---|---|
| Project shown Ready with missing content sets | The project does not exist until all N sets have validated. |
| Partial project silently published | Same. Partial state lives only in `UploadSession` rows. |
| Duplicate project if the final request is retried | The all-N-or-none claim; a replay of a completed group returns the same `project_id` with `recovered_existing_completion: true`. |
| Content-set numbering changing after resume | `pair_index` comes from the finalize request's array order, fixed client-side before the first byte and persisted in the localStorage `sets` array. Test 17 uploads out of order on purpose and asserts the order still holds. |
| Orphan pair rows after failure | Pairs are only ever created inside the transaction that commits the whole project. |
| Quota charged for content that never finalized | Quota, storage allowance and ledger rows are all taken at finalize. Test 16 asserts all three are untouched mid-upload. |

Partial state is now **explicit** rather than silently broken: every session
reports a derived `set_state`, and the group finalize's 409 responses carry the
full per-set array.

## 8. Per-content-set state

Derived from columns that already exist — `status`, `current_offset`,
`expected_total_size` — and exposed as `set_state` on every session payload.
**No new column. No migration.**

| `set_state` | Derived from |
|---|---|
| `pending` | `active`, offset 0 |
| `uploading` | `active`, `0 < offset < total` |
| `uploaded` | `active`, `offset == total` (byte-complete, waiting on siblings) |
| `finalizing` | `finalizing` or `assembled` |
| `complete` | `completed` |
| `failed_requires_action` | `failed`, `expired` or `cancelled` |

**`paused` is deliberately absent from the server vocabulary.** Server-side, a
paused upload and a very slow one are the same row: `active`, partial offset.
A server-reported `paused` would be a guess the client would then have to keep
in step with — the exact class of bug Phase 1 found in its own dead
`experience_type` comparison. Pausing is a client fact and stays one; the
client renders `paused at 42%` from its own record. What the server owns is the
offset, and it reports that.

## 9. Server-authoritative resume

Unchanged contract, applied per set. Each set's chunk loop starts from the
**server's** `current_offset`, never a local counter — `uploadResumableStream()`
reads `session.current_offset`, and `stored.currentOffset` is never used for
control flow (the comment saying so is still there and still true).

For a group, the group finalize is the second authority: if any set is short it
returns `409 INCOMPLETE_UPLOAD` with **every** set's authoritative offset and
`set_state`, and the client re-sends only the short ones from the offsets the
server just gave it. Tests 3, 5, 6 and 14 assert this; test 6 also confirms a
chunk sent at a believed-but-wrong offset is rejected with the true offset
inline (no extra round trip, per Phase 1's design).

## 10. Refresh recovery

The **same** persistence mechanism, extended — not a second one. Key bumped to
`scanstory.resumableUpload.v3` (the record gained required fields; a v2 record
fails the version check and is ignored). The flat single-pair fields stay
exactly as Phase 1 wrote them, and the record gains:

| Field | Why |
|---|---|
| `sets[]` | one record per content set, in upload order |
| `setIndex` | which set the flat cursor currently describes |

Each `sets[i]` holds `sessionId`, `imageName/Size`, `videoName/Size`,
`videoLastModified`, `imageFingerprint`, `videoFingerprint`,
`expectedTotalSize`, `currentOffset` (UI bookkeeping only), `complete`.
`saveResumableUploadState()` syncs the active set's `currentOffset` from the
flat cursor on every write, so one write persists both views of the same fact.

Project-level identity (`projectName`, `experience_type`, `playback_mode`) stays
where it was, checked once. Still nothing sensitive: no auth token, no CSRF
token, no URL that grants access — every route re-checks ownership server-side
and 404s a session that is not yours.

On load, per set, in order:

1. read the saved record; require version, project name, experience type,
   playback mode **and set count** to match;
2. check that set's file identity via the real `storedSessionMatchesFiles`
   (called with the per-set record spread over the project record);
3. `GET` that set's session — **the server's answer wins in every branch**:
   `completed` + `project_id` → the whole project already exists (all sets
   settle together), redirect to it; `active` → resume from the server's
   offset; `assembled` → group finalize retries only the enqueue; terminal →
   that one set starts clean;
4. identity mismatch → **that one set** gets a fresh session and its stale
   session is cancelled to release its temp bytes; every other set is
   untouched.

A local "100%" the server does not confirm is a local belief and nothing more,
for **any** set. Tests 4, 5, 6 and 8 cover this.

## 11. File reselection behaviour

Handled honestly, because a page genuinely cannot hold `File` handles across a
reload.

On load, if a saved v3 record has any incomplete set, the page states it up
front rather than leaving the creator to guess:

> **Re-select the same video to continue your upload. Your uploaded progress is
> safe.**

After reselection: fingerprint per set (SHA-256 over the first and last 64 KB
via `crypto.subtle`, Phase 1's function, unchanged) →

- **match** → resume that set from the server's offset;
- **mismatch** → the unsafe append is blocked; **only that set** restarts, its
  stale session is cancelled, and completed sets are never touched.

The asymmetry is deliberate and unchanged from Phase 1: a false negative costs
a restart (safe), a false positive would corrupt media (not safe).

Nothing claims background upload survives a closed browser. The copy is careful
about that, as Phase 1's was.

## 12. Fingerprint behaviour

Unchanged mechanism, applied per set. `fileFingerprint()` and
`fingerprintsMatch()` are Phase 1's, untouched: name + size + lastModified plus
head/tail SHA-256, degrading to metadata-only when SubtleCrypto is unavailable
rather than losing the ability to resume at all.

What is new is the **scope**: identity is checked per content set, so one
replaced file invalidates one set. Test 7 executes the real shipped matcher in
Node across three sets where only the middle one's file changed, and asserts
`[True, False, True]`.

## 13. Completed-set preservation

Three independent mechanisms, all tested:

1. **Client skip.** A set whose record is `complete`, or whose server offset
   equals its total, is skipped with a `RESUMABLE CLIENT SET SKIPPED` log — no
   bytes sent.
2. **Server replay guard.** If the client re-sends anyway, the existing
   fully-contained-replay branch answers `duplicate_chunk_ignored` with the
   unchanged offset. Test 8 replays set 1 from offset 0 and asserts the
   assembled bytes are byte-identical before and after.
3. **Failure isolation.** A sibling failing never deletes a completed set's
   assembled temp file (§15).

Test 3 asserts set 1's assembled bytes are still byte-identical to the source
after set 2 was interrupted and resumed. The measurement harness asserts
`sets_needing_resend_after_reconcile == 0` for both multi-set runs.

## 14. Duplicate / replay behaviour

Every case in the brief, and what actually happens:

| Case | Result |
|---|---|
| Duplicate chunk within set 1 | `200` + `duplicate_chunk_ignored`, offset unchanged, zero bytes appended (test 8) |
| Duplicate chunk within set 2 | Same, ×4 in a row, and set 1 undisturbed (test 9) |
| Duplicate content-set finalize | A `project_content_set` session refuses the single-session route entirely (`GROUP_FINALIZE_REQUIRED`, test 10b) |
| Duplicate group finalize | Second call returns `200` with `recovered_existing_completion: true` and the **same** `project_id` (test 10) |
| Duplicate overall project finalize ×3 | One project, N pairs, ≤1 job, one quota unit (test 11) |
| Browser resending after response loss | Client reconciles via `GET` before deciding; a completed group is recognised as success |
| Refresh triggering reconciliation twice | Reconciliation is a `GET` per set — idempotent by construction |
| Double-click Create | The all-N-or-none claim; the loser gets `409 FINALIZE_IN_PROGRESS`, and exactly one project exists at the end (test 12) |
| Processing-scheduling replay | One enqueue call for N sets, and `enqueue_project_pair_processing` dedupes against an active job (test 20) |

Result in every case: **one logical project, N intended content sets, one media
set per content set, one processing lifecycle.** Never more.

One finding the tests produced rather than confirmed: a group already past the
claim gate originally answered `INCOMPLETE_UPLOAD`, which reads as "send more
bytes" when the truth is "another request holds this". Test 12 caught it; the
route now answers `FINALIZE_IN_PROGRESS` and hands back the per-set state.

## 15. Failure isolation

If content set 3 fails validation:

- set 1 stays safe — `active`, assembled temp file byte-identical;
- set 2 stays safe — same;
- set 3 is `failed` with its `failure_code`, its temp file removed, and it is
  the one thing the creator has to replace;
- the response names it: `failed_session_id` and `failed_set_index`;
- no project exists, no quota is consumed, no ledger row is written;
- the client says so: *"Content set 3 could not be used: … Replace just that
  file — every other content set you already uploaded is safe."*

The creator then creates **one** new session for that set and finalizes the
group again. Test 15 walks exactly that and ends with a 3-pair project.

The product rule from §5 (all sets before project creation) is preserved, and
completed sets' temp-upload state is retained rather than discarded — which is
the whole point.

**Where isolation stops, stated plainly.** Failures *after* the assembled temp
files have been consumed by validation — storage allowance, project quota,
project creation — are terminal for the whole group. By then there are no
confirmed bytes left to preserve for anyone, and marking a set resumable when
its bytes are gone would be a lie the next finalize would expose as
`STORAGE_INCONSISTENT`. This matches the single-pair behaviour exactly. The one
case worth catching early is caught early: `PAIR_LIMIT_REACHED` is refused
*before* the claim, so every set keeps its bytes and a plan upgrade plus Resume
works (test 16b).

## 16. Long-pause recovery

Phase 1 flagged this precisely: *"a paused upload's real lifetime is bounded by
`SCANSTORY_UPLOAD_SESSION_ABANDONED_STALE_MINUTES` (default 2h), not the
headline TTL."*

**The audited cause.** Both windows are inactivity-based, and the cleanup query
is `or_(expires_at < now, updated_at < abandoned_cutoff)`. So the *shorter*
binds. TTL 1440, stale window 120 → a pause longer than two hours was
reclaimable, while the UI implied a day.

**The narrow fix — that one default, not every timeout.**
`SCANSTORY_UPLOAD_SESSION_ABANDONED_STALE_MINUTES` now defaults to
`UPLOAD_SESSION_TTL_MINUTES` (1440 = 24 h), so the advertised window is the
real one. Cleanup stays inactivity-based, still only touches `status='active'`
rows, still bounded by `--limit`, still dry-run by default. An operator under
temp-storage pressure can still set it lower deliberately. Genuinely abandoned
uploads are still reaped — after a day of true inactivity instead of two hours.

24 h was chosen rather than 48–72 h because the temp-storage retention
assumption is local disk under `TMP_UPLOADS_DIR` with no separate lifecycle
policy, and a longer window multiplies unaccounted temp bytes with no
compensating mechanism (§18). It is one environment variable away if an
operator has the disk.

**A second gap this pass found, specific to multi-set.** A set that finishes its
bytes early then goes completely quiet while its siblings crawl. Three 20 MB
sets at 0.3 Mbps is a multi-day group upload, and set 1 would be reaped for
"inactivity" while the group is actively progressing. So an owner's status
`GET` on an `active` session now slides that session's deadline. A read from
the owner is proof the creator is still there; the client has to make that read
anyway to honour server-authoritative state, so the read *is* the keepalive —
no new endpoint, no new column. An already-expired session is never resurrected
by it (test 22 asserts that).

Active slow uploads are never killed for wall-clock duration: both windows key
off activity, never off `created_at`. Test 21 asserts
`ABANDONED_STALE >= TTL >= 24 h`, that a 20-hour pause survives
`cleanup --apply` with bytes intact, and that a status read pushes a
5-minutes-from-expiry deadline back out past 20 hours.

## 17. Cleanup behaviour

| Aspect | Behaviour |
|---|---|
| Session TTL | `expires_at`, inactivity-based. Slid by every accepted chunk **and** every owner status read. Default 1440 min. |
| Cleanup staleness rule | `status='active'` **and** (`expires_at < now` **or** `updated_at < now - ABANDONED_STALE`). Default 1440 min. Ordered by id, `--limit`-bounded, dry-run unless `--apply`. |
| Temp-file cleanup | `_safe_delete_upload_temp()` — path-validated inside `TMP_UPLOADS_DIR`. On expiry, on cancel, on per-set validation failure (offender only), and after successful validation. |
| DB-row cleanup | Rows are never deleted, only transitioned to `expired` with `failure_code` (`SESSION_TTL_EXPIRED` / `SESSION_ABANDONED_STALE`). They are operational history and survive project deletion via `ondelete="SET NULL"`. |
| Still not swept | A session stuck in `finalizing`. Pre-existing, documented by Phase 1, unchanged here. |

Test 22 asserts a genuinely abandoned pair transitions to `expired`, has its
temp files deleted, is refused cleanly on finalize, and reads back as
`failed_requires_action`.

## 18. Quota / storage behaviour

Verified in code and asserted in tests, not guessed.

**During a pause: nothing is charged.** A non-finalized session holds bytes only
in `TMP_UPLOADS_DIR`. No project-quota unit, no account storage allowance, no
`MediaObject` ledger row. Test 16 asserts `projects_used == 0` and
`account_storage_state()` unchanged while three fully-uploaded sets sit waiting.

**At finalize, once, for the whole project.** `reserve_account_storage(user.id,
summed_bytes, allowance)` → `_reserve_project_quota_atomic(user)` **once** →
`_reserve_pair_slots_for_project(project.id, N, max_pairs)` → 2N `MediaObject`
rows in the same transaction. Test 16 asserts one project unit for three sets,
six ledger rows, and that the ledger total equals the account's storage
increase exactly.

**The implication of a longer recoverable-pause window, stated rather than
glossed:** up to a day of unaccounted temp bytes per abandoned upload, bounded
by nothing but disk. `cleanup-upload-sessions --apply` therefore has to actually
be scheduled — it is dry-run by default and does nothing if nobody runs it.
This is now in the contract doc as an operational requirement.

## 19. Processing behaviour

One `_schedule_project_pair_processing(project.id)` for the whole project,
however many content sets fed it — the same RQ enqueue the multipart route
uses, unmodified. `enqueue_project_pair_processing` is per project and dedupes
against an active job.

`_finalize_enqueue_and_complete()` now takes the session list and settles all
rows together: all `completed` on success, all `assembled` +
`QUEUE_ENQUEUE_FAILED` on failure. Re-finalizing an all-`assembled` group
retries **only** the enqueue — no re-validation, no second quota unit, no
duplicate project or pairs (test 20b).

`direct_qr` still enqueues nothing and marks every pair
`is_processed=True` / `feature_extraction_status="not_required"` (test 24).

Test 20 counts enqueue calls: **1** for three content sets, and still 1 after
three finalize replays.

## 20. Creator UX

- **Per-content-set progress.** A live list beside the bar:
  `Content set 1 of 3 — complete` / `Content set 2 of 3 — 58% uploaded` /
  `Content set 3 of 3 — waiting`. Shown only for multi-set projects. No offsets
  and no chunk sizes anywhere in it — those are protocol details, and a creator
  asked to reason about them has been handed our problem.
- **Paused shows Resume.** Phase 1's `pauseUploadForNetwork()` is reused:
  *"Your connection dropped. Your uploaded progress is safe."* and the button
  becomes **Resume upload**. The per-set list keeps showing which sets are done
  and where the active one stopped.
- **Refresh needing reselection** shows the required copy verbatim:
  *"Re-select the same video to continue your upload. Your uploaded progress is
  safe."*
- **A rejected set names itself** and says the others are safe (§15).
- **Nothing promises a closed tab keeps uploading.** Same honesty rule as
  Phase 1.

## 21. Focused tests

Focused only — the full 1900+ regression was **not** run, per policy.

| Suite | Tests | Result |
|---|---|---|
| `tests/integration/test_multi_pair_resumable_upload.py` (new) | 32 | pass |
| `tests/integration/test_resumable_upload.py` (Wave 5) | 40 | pass |
| `tests/integration/test_extreme_low_bandwidth_upload.py` (Phase 1) | 26 | pass |
| `tests/gate_jr/test_marker_selection_upload.py` | 65 | 64 pass, 1 skip (Playwright) |
| `tests/gate_jr/test_v11_experience_ux.py` | 34 | pass |
| **Total** | **197** | **196 pass, 1 skip, 0 fail** |

**All 25 required scenarios are covered**, plus 7 extra that the design or the
failures made necessary:

| # | Requirement | Test |
|---|---|---|
| 1 | 2-set normal creation | `test_01_two_set_normal_creation` |
| 2 | 3-set normal creation | `test_02_three_set_normal_creation` |
| 3 | set1 complete / set2 interrupted / resume | `test_03_set1_complete_set2_interrupted_then_resumes` |
| 4 | refresh after set1 complete | `test_04_refresh_after_set1_complete_recovers_group_state` |
| 5 | refresh midway set2 | `test_05_refresh_midway_set2_resumes_from_server_offset` |
| 6 | server state overrides local | `test_06_server_state_overrides_local_belief_for_every_set` |
| 7 | fingerprint mismatch one set only | `test_07_fingerprint_mismatch_rejects_only_the_changed_set` (real JS in Node) |
| 8 | completed set not re-uploaded | `test_08_completed_set_is_never_reuploaded` |
| 9 | duplicate chunk on set2 | `test_09_duplicate_chunk_on_set_two_is_idempotent` |
| 10 | duplicate finalize on set2 | `test_10_duplicate_finalize_of_the_group_creates_one_project` + `test_10b_a_content_set_cannot_be_finalized_as_its_own_project` |
| 11 | duplicate overall completion | `test_11_triple_project_finalize_produces_one_project_and_one_job` |
| 12 | double-click Create | `test_12_double_click_create_finalizes_exactly_once` |
| 13 | connection loss between sets | `test_13_connection_loss_between_sets_preserves_the_finished_one` |
| 14 | connection loss during set3 | `test_14_connection_loss_during_set3_leaves_sets_one_and_two_intact` |
| 15 | failed set doesn't destroy prior completed | `test_15_failed_set_does_not_destroy_prior_completed_sets` |
| 16 | quota / storage accounting | `test_16_quota_and_storage_accounting_is_correct_for_a_group` + `test_16b_pair_limit_is_refused_before_anything_is_claimed` |
| 17 | content-set order preserved | `test_17_content_set_order_is_preserved_as_pair_index` |
| 18 | exact expected pair count | `test_18_project_has_exactly_the_expected_pair_count` (parametrised 1–4) |
| 19 | no orphan media rows | `test_19_no_orphan_media_rows_on_success_or_failure` |
| 20 | no duplicate processing jobs | `test_20_no_duplicate_processing_jobs_for_a_multi_set_project` + `test_20b_enqueue_failure_parks_every_set_and_retries_only_the_enqueue` |
| 21 | paused session survives configured period | `test_21_paused_session_survives_the_configured_inactivity_period` |
| 22 | genuinely abandoned cleanup | `test_22_genuinely_abandoned_session_is_still_cleaned_up` |
| 23 | explicit cancel | `test_23_explicit_cancel_releases_only_what_was_cancelled` |
| 24 | direct_qr unaffected | `test_24_direct_qr_multi_set_is_unaffected` + `test_24b_mixed_experience_types_cannot_be_finalized_as_one_project` |
| 25 | single-pair path remains green | `test_25_single_pair_resumable_path_remains_green` **plus Phase 1's actual 26-test file and Wave 5's 40-test file, both run and both green** |

The load-bearing tests assert against the assembled bytes **on disk**, not
against `current_offset` — an offset assertion would pass for a splicing bug.

**Two gate_jr guards re-pointed**, both legitimately. One asserted the
*opposite* architecture (that multi-pair deliberately used the monolithic
uploader) — the exact gap this series closed, so it now pins what replaced it,
including that the multipart fallback is gated on browser capability rather than
pair count. The other pinned `createResumableSession`'s exact argument list,
which grew a trailing `purpose`.

## 22. Larger-file measurements

`scripts/dev/multi_pair_upload_measurement.py`; raw results in
`docs/development/phase2_multi_pair_upload_measurements.json`.

Phase 1 could not get past a 512 KiB payload — 2 MiB reliably killed the CDP
socket in that environment. So this harness drops Chrome entirely and drives the
real `/api/uploads` routes in-process, generating every byte at runtime rather
than committing a 50 MB fixture to git.

**It is explicit about which numbers are real.** Chunk-size evolution, retry
count, retransmitted bytes, server-offset correctness, session expiration, peak
traced memory and final byte integrity are all real behaviour of the shipped
code. **The duration is analytic** — `size/rate` plus a fixed 0.15 s
per-request latency, fed to the real adaptive sizer exactly as a measured
sample would be. It is reported so the *shape* of a 20 MB transfer at 0.3 Mbps
is visible and must never be quoted as a measured production number. The
harness also asserts the shipped adaptive-chunk constants still match the ones
it mirrors, so it can never silently measure a policy the product no longer
ships.

| Payload | Link | Requests | Chunk first → final | Retransmitted | Peak traced mem | Offset correct | Bytes identical | Analytic duration |
|---|---|---|---|---|---|---|---|---|
| 5 MB | 1.0 Mbps | 7 | 128 KiB → 832 KiB | 0 B | 6.8 MB | yes | yes | 0.7 min |
| 5 MB | 0.6 Mbps | 11 | 128 KiB → 512 KiB | 0 B | 4.5 MB | yes | yes | 1.2 min |
| 5 MB | 0.3 Mbps | 21 | 128 KiB → 256 KiB | 0 B | 5.5 MB | yes | yes | 2.4 min |
| 20 MB | 0.6 Mbps | 41 | 128 KiB → 512 KiB | 0 B | 16.3 MB | yes | yes | 4.8 min |
| 20 MB | 0.3 Mbps | 81 | 128 KiB → 256 KiB | 0 B | 10.0 MB | yes | yes | 9.5 min |
| 50 MB | 0.6 Mbps | 101 | 128 KiB → 512 KiB | 0 B | 21.7 MB | yes | yes | 11.9 min |

Multi-set runs:

| Group | Link | Total | Re-sends after reconcile | All sets intact | Failure isolation post-finalize |
|---|---|---|---|---|---|
| 3 × 5 MB | 0.6 Mbps | 15 MB | **0** | yes | offender `failed`, both siblings `active` with full assembled bytes |
| 2 × 20 MB | 0.3 Mbps | 40 MB | **0** | yes | offender `failed`, sibling `active` with full assembled bytes |

Chunk growth is correctly bounded by the link, not the file: 0.3 Mbps settles at
256 KiB, 0.6 at 512 KiB, 1.0 at 832 KiB — the adaptive sizer targeting ~8 s per
chunk, as designed. Memory tracks chunk size and request buffering, not file
size, at every payload up to 50 MB.

## 23. 0.6 Mbps result

5 MB completed in 11 requests, 20 MB in 41, 50 MB in 101 — all with **zero
retransmitted bytes**, correct final server offsets and byte-identical assembly.
Chunk size converged to 512 KiB in two steps from the 128 KiB floor and stayed
there; no oscillation. The session stayed `active` throughout and its
inactivity deadline was extended by every accepted chunk.

## 24. 0.3 Mbps result

5 MB completed in 21 requests, 20 MB in 81 — zero retransmitted bytes,
byte-identical assembly, correct offsets. Chunk size converged to 256 KiB, i.e.
the sizer correctly declined to grow to the 512 KiB it chose at 0.6 Mbps. The
2 × 20 MB group run at this speed needed zero re-sends after reconciliation.

## 25. 20 MB result

Both 20 MB runs completed cleanly (41 requests at 0.6 Mbps, 81 at 0.3 Mbps),
zero retransmitted bytes, byte-identical assembly, peak traced memory 16.3 MB
and 10.0 MB — bounded by chunk size and request buffering, not by file size.
The 2 × 20 MB group run also confirmed failure isolation at this scale: the
rejected set went `failed`, the sibling came back `active` with its full 20 MB
assembled temp file intact on disk.

## 26. 50 MB result

Run, and it completed: 101 requests at 0.6 Mbps, zero retransmitted bytes,
byte-identical 50 MB assembly, correct final server offset, session still
`active` with its deadline extended, peak traced memory 21.7 MB. Chunk size
converged to 512 KiB in two steps and held for all 101 requests — sustained
growth over ~100 chunks, which is what Phase 1's 512 KiB browser payload could
not demonstrate.

## 27. Migration status

**No migration. No schema change. Nothing added to Alembic.** This was a design
constraint and it held.

Two things that could have needed one, and how they did not:

1. **Per-content-set state.** Derived from `status`, `current_offset` and
   `expected_total_size` (§8). Checked before concluding otherwise: the
   `UploadSession.status` enum plus the two offset columns already express every
   state in the brief's list.
2. **Grouping N sets into one project.** The group is the finalize request's
   `session_ids` array — no parent table, no group-id column (§6). Atomicity
   comes from a conditional UPDATE over `id IN (...)`.

One value was added to an existing column: `purpose` gained
`"project_content_set"` alongside `"project_pair"`. `purpose` is
`db.Column(db.String(30))` validated by a Python `@validates` against
`UPLOAD_SESSION_PURPOSES`, and — checked explicitly in `__table_args__` —
**no CHECK constraint covers it** (the constraints are on `current_offset`,
the sizes, `experience_type` and `playback_mode`). So this is a new allowed
value, not a schema change, and no existing row is affected.

## 28. Scanner hashes before / after

LF-normalised SHA-256 (Phase 1's approach, avoiding the CRLF-checkout false
positive it documented).

| File | Before | After |
|---|---|---|
| `scanner_runtime.py` | `eda140bf24f534e160d365c863c618469d68bbcf9619273d499674590324cec0` | `eda140bf24f534e160d365c863c618469d68bbcf9619273d499674590324cec0` |
| `static/js/scanner-runtime.js` | `05badbd03e00c22715edbdba168db8721ae621493acab8a211a54dbf76acc5b2` | `05badbd03e00c22715edbdba168db8721ae621493acab8a211a54dbf76acc5b2` |

**Identical.** No ORB/RANSAC/homography/optical-flow/tracking-geometry/matching-threshold/camera-calibration/overlay-math
change. No pricing, coverage, ownership, vendor-rule, refund, payment-rule or
plan-limit change. No S3/R2/HLS/service-worker/multi-bitrate work.

## 29. `git diff --check`

```
$ git diff --check
$ git diff --check 29eb9fa..HEAD
```

Clean — no whitespace errors, no conflict markers, in the working tree or
across the whole range.

## 30. `git status --short`

```
$ git status --short
$ git status
On branch agent/v1.1-platform-admin
nothing to commit, working tree clean
```

Empty. Every file this pass touched is committed and nothing is left behind -
no stray scratch files, no uncommitted edits. `git diff --name-only
29eb9fa..HEAD` lists exactly the nine intended files and nothing else.

## 31. Remaining limitations

Honest list, including what this pass chose not to do.

1. **No marker-crop provenance on the resumable path** — and now that every
   JS-driven creation is resumable, that reaches multi-set projects too. A
   resumable-created pair gets `marker_mode` at its model default
   (`full_image`) and null crop fields. These are diagnostic/provenance only:
   `app.py`'s own comment establishes that the uploaded pixels are always the
   authoritative marker and `marker_crop_*` is *"never an instruction to crop
   again"*, applied solely via the explicit
   `rebuild_pair_features(..., apply_legacy_roi=True)` repair path. Skipped
   deliberately rather than overlooked: closing it means either a migration
   adding `marker_*` to `upload_sessions` (which the brief said to flag rather
   than add) or accepting the metadata in the group-finalize body (~60 lines
   for a field whose only consumers are a diagnostic string and an admin repair
   flag). **This is the one behaviour change in this pass that a reader should
   weigh before shipping.**
2. **Durations in §22–26 are analytic, not wire-measured.** The protocol
   behaviour is real; the clock is simulated. No production minimum bandwidth
   is claimed from it, and none should be.
3. **No real-radio data.** Unchanged from Phase 1. Loopback with emulated
   latency is not a cellular link.
4. **Quota / storage / project-creation failures are group-terminal** (§15).
   Isolation covers per-set content failures, which is the common case, not
   these.
5. **A paused upload's real lifetime is now 24 h and depends on cleanup being
   scheduled.** Honest for overnight, not for a week. Raising it is one env var,
   but §18's temp-byte cost is the reason it was not raised further unasked.
6. **A session stuck in `finalizing` is still not swept** by the cleanup CLI.
   Pre-existing, documented by Phase 1, unchanged.
7. **Closing the tab still stops the upload.** Fixing it needs a service
   worker, explicitly out of scope. The copy never promises otherwise.
8. **"Attach a resumable content set to an existing project" is still out of
   scope.** A finalize always creates a new project.
9. **The legacy multipart XHR client path still exists**, now reachable only
   for a browser lacking `fetch`/`AbortController`/`Blob.slice`. Kept as a real
   fallback rather than deleted; on such a browser a multi-set upload is still
   all-or-nothing.
10. **Per-chunk integrity still deferred**, with Phase 1's reasoning unchanged:
    it would add bytes to every request on exactly the links that can least
    afford them, to catch a corruption mode the optional whole-file checksum
    already catches.
11. **Test 7 skips if Node is absent.** It ran here; a CI image without Node
    would silently lose that coverage — same caveat Phase 1 recorded.
12. **Auto-resume still needs the browser's `online` event.** A link that
    degrades to uselessness without going formally offline pauses and waits for
    a human.

## 32. Recommendation

**Ship it.** The gap Phase 1 named as its largest is closed, the pause-window
trap it flagged is fixed, and the single-pair path it built is provably
untouched — 66 of its own tests and 99 gate_jr client guards green, on the same
generalised code path the multi-set flow uses.

Two things to decide before or shortly after release, in order:

1. **Marker provenance (limitation 1).** This is the one item where behaviour
   changed for multi-set projects without an accompanying fix. It is
   diagnostic-only and the geometry is unaffected, but it is a deliberate
   trade-off recorded here rather than a silent one, and it deserves a yes/no
   rather than drifting. The cheap close is accepting `marker_meta` in the
   group-finalize body — no migration.
2. **Schedule `cleanup-upload-sessions --apply`.** The 24-hour recoverable
   pause is real only if cleanup runs, and the temp-byte cost in §18 is real
   only if it does not. It is dry-run by default and manual today; a daily
   invocation is the whole ask.

Then, in priority order: field measurement on a real radio with §22's
telemetry, which is what turns "0.3 Mbps completed in a lab" into a supportable
minimum; sweeping `finalizing` sessions in the cleanup CLI; and only then
per-chunk integrity, and only if a real corruption incident justifies the bytes
it costs.
