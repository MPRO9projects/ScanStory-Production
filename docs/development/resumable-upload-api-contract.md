# Resumable Upload API Contract (V1 Wave 5, extended V1.1 Phase 2)

Wire contract for the resumable upload API, written so an implementer can build
against it without reading `app.py`. The shipped frontend
(`templates/user/user_create_project.html`) is a consumer of this contract, not
its definition - V1.1 Phase 1 built the single-set client, Phase 2 the
multi-content-set one.

## Scope

One `UploadSession` = one **content set**: exactly one image and one video,
uploaded as a single sequential byte stream. The client sends the image's bytes
first (`image_size` bytes), then immediately the video's bytes (`video_size`
bytes) - back to back, no separator. `expected_total_size = image_size +
video_size`.

**V1.1 Phase 2 - multi-content-set projects.** A project may now be built from
N content sets, each its own independently resumable session:

- Each set is created with `"purpose": "project_content_set"` and uploaded
  through the same chunk route as any other session.
- The whole project is created by ONE call to
  `POST /api/uploads/projects/finalize` with the ordered `session_ids`. That
  request's order becomes `pair_index` 0..N-1.
- The "group" is defined by the finalize request, not by a parent row and not
  by a new column: there is no draft-project table and no migration. All the
  atomicity comes from one conditional UPDATE that must claim exactly N rows.
- A session created with the default `"purpose": "project_pair"` still
  finalizes on its own through route 4 and still produces a single-pair
  project. A `project_content_set` session is REFUSED by route 4
  (`409 GROUP_FINALIZE_REQUIRED`) so a client bug cannot turn content set 2 of
  3 into a stray one-pair project with its own quota unit.
- Attaching a resumable content set to an ALREADY EXISTING project is still out
  of scope. A finalize always creates a brand-new project.

**Project atomicity.** No Project row exists until every set in the group is
byte-complete AND every set validates. That is the same all-or-nothing
guarantee the non-resumable multipart `/upload` route has always had; partial
progress lives entirely in `UploadSession` rows, where it is explicit and
recoverable rather than a half-built project.

Authentication: every route requires either a logged-in user session or a
logged-in Admin session (whichever the existing app already uses -
`session["user_id"]` / `session["admin_id"]`). Missing both returns
`401 UNAUTHENTICATED`. Ownership is enforced on every route past creation: a
session can only ever be read/mutated by the identity that created it. A
mismatched or unauthenticated caller always gets `404 NOT_FOUND` (never a
403) so a session's existence is never leaked to a caller who doesn't own it.

CSRF: these are normal authenticated POST routes, not exempted from this
app's global CSRF protection. Send the CSRF token via the `X-CSRFToken` or
`X-CSRF-Token` header (already-configured `WTF_CSRF_HEADERS`).

## Status enum

| Status       | Meaning |
|--------------|---------|
| `active`     | Accepting chunks. |
| `finalizing` | Transient - only ever observed mid-request, guards against double finalization. A client should never see this at rest. |
| `assembled`  | File(s) fully validated, Project+ProjectPair created, quota consumed - but the RQ enqueue call itself failed. Recoverable: call finalize again to retry ONLY the enqueue step. |
| `completed`  | Fully done: validated, persisted, and queued for processing. |
| `cancelled`  | Client cancelled before completion. |
| `expired`    | TTL passed, or found stale by the cleanup CLI, before completion. |
| `failed`     | Validation, quota, or project-creation failure. See `failure_code`. |

### Derived per-content-set state (`set_state`)

Every session payload also carries `set_state`, computed from `status`,
`current_offset` and `expected_total_size` - **no stored column, no
migration**:

| `set_state` | Means |
|---|---|
| `pending` | `active`, nothing uploaded yet. |
| `uploading` | `active`, partially uploaded. |
| `uploaded` | `active`, byte-complete, waiting for its siblings / for finalize. |
| `finalizing` | `finalizing` or `assembled`. |
| `complete` | `completed`. |
| `failed_requires_action` | `failed`, `expired` or `cancelled`. |

There is deliberately no `paused`. A paused upload and a very slow one are the
same row server-side; pausing is a client fact and stays one. What the server
owns is the offset, and it reports that.

## Routes

### 1. Create session

`POST /api/uploads/sessions`

Request body (JSON):

```json
{
  "image_size": 123456,
  "video_size": 98765432,
  "project_name": "My Project",
  "original_image_name": "marker.jpg",
  "original_video_name": "overlay.mp4",
  "image_content_type": "image/jpeg",
  "video_content_type": "video/mp4",
  "client_checksum_sha256": "optional 64-hex-char sha256 of the FULL image+video byte stream"
}
```

Required: `image_size`, `video_size` (positive integers, in bytes). Everything
else is optional. `original_image_name`/`original_video_name`/`project_name`
are stored for display only - they are never used to build a filesystem path.

Validation, in order, before anything is allocated:
- `image_size`/`video_size` must be positive integers -> `400 INVALID_SIZE`
- `image_size > MAX_IMAGE_UPLOAD_BYTES` -> `400 IMAGE_TOO_LARGE`
- `video_size > MAX_VIDEO_UPLOAD_BYTES` -> `400 VIDEO_TOO_LARGE`
- combined size over `MAX_CONTENT_LENGTH` (only if that env var is set) -> `400 TOTAL_TOO_LARGE`
- malformed `client_checksum_sha256` -> `400 INVALID_CHECKSUM`
- for user owners only (never checked for Admin owners): blocked account ->
  `403 ACCOUNT_BLOCKED`; project limit reached -> `403 PROJECT_LIMIT_REACHED`;
  plan has no pairs-per-project configured -> `403 PLAN_NOT_CONFIGURED`;
  general subscription/trial limit -> `403 SUBSCRIPTION_LIMIT`

Success: `201`

```json
{
  "success": true,
  "session": {
    "id": 42,
    "status": "active",
    "purpose": "project_pair",
    "current_offset": 0,
    "expected_total_size": 99000000,
    "uploaded_bytes": 0,
    "remaining_bytes": 99000000,
    "progress_percent": 0,
    "image_size": 123456,
    "video_size": 98765432,
    "project_id": null,
    "pair_id": null,
    "pair": null,
    "processing_job": null,
    "failure_code": null,
    "can_upload_chunks": true,
    "can_finalize": false,
    "can_retry_finalize": false,
    "can_cancel": true,
    "is_terminal": false,
    "created_at": "...", "updated_at": "...", "expires_at": "...", "completed_at": null
  }
}
```

`expires_at` starts at `created_at + 1440 minutes` (24h) by default
(`SCANSTORY_UPLOAD_SESSION_TTL_MINUTES`) - generous on purpose: large video
files over slow/mobile networks may legitimately take a long time to fully
upload in chunks. Compare the payment-reservation TTL (30 min) which gates a
much shorter checkout flow; a resumable upload's TTL is deliberately a
different order of magnitude for a different reason (slow transfer, not
checkout abandonment).

**`expires_at` is an inactivity deadline, not a wall clock.** Every accepted
chunk slides it forward by the full TTL (V1.1 extreme-low-bandwidth
hardening). A creator who pauses an upload overnight on a bad mobile link
must still be able to resume the bytes the server has already confirmed, and
a wall clock measured from session creation would take those bytes away for
no protocol reason. Genuinely abandoned sessions are still reaped by
`cleanup-upload-sessions`, which keys off the much shorter `updated_at`
staleness window (`SCANSTORY_UPLOAD_SESSION_ABANDONED_STALE_MINUTES`,
default 120) - that window, not the TTL, is what bounds how long a paused
upload survives, so set it to the pause window you actually want to support.

`max_chunk_bytes` in the session payload publishes the server's configured
chunk ceiling (`SCANSTORY_RESUMABLE_CHUNK_MAX_BYTES`). A client that sizes
chunks adaptively must read it from here rather than hardcoding a copy of
the default, which would silently start returning `413` the day the config
is lowered.

### 2. Upload next sequential chunk

`POST /api/uploads/sessions/<id>/chunk`

- Header `X-Chunk-Offset`: required, non-negative integer - the byte offset
  this chunk claims to start at.
- Body: raw bytes of the chunk (`Content-Type: application/octet-stream`).

Offset rules (sequential-only, V1):
- `X-Chunk-Offset == current_offset`: normal case. Bytes are appended; offset
  advances by the chunk's length. Rejected with `400
  CHUNK_EXCEEDS_EXPECTED_SIZE` if the new offset would exceed
  `expected_total_size`.
- `X-Chunk-Offset < current_offset` **and** `X-Chunk-Offset + len(body) <=
  current_offset`: this exact chunk was already accepted (a network retry of
  a request whose response the client never saw). Treated as an **idempotent
  no-op success** - `200` with `"note": "duplicate_chunk_ignored"` and the
  unchanged `current_offset`. This is the retry-safety guarantee: resending
  the same already-accepted chunk is always safe.
- Any other offset (a gap, or an overlap that isn't a clean retry of already-
  accepted bytes): `409 OFFSET_MISMATCH`. A **partial** overlap is
  deliberately rejected rather than spliced: accepting one would mean
  trusting that the overlapping prefix is byte-identical to what is already
  on disk, which nothing in this protocol proves.
- Empty body: `400 EMPTY_CHUNK`.
- Body larger than `SCANSTORY_RESUMABLE_CHUNK_MAX_BYTES` (default 1 MiB):
  `413 CHUNK_TOO_LARGE`.

**Recoverable rejections carry their own recovery data** (V1.1
extreme-low-bandwidth hardening), so a client on a weak uplink does not
spend a second round-trip asking what it could have been told the first
time:

| Response | Extra fields |
|---|---|
| `409 OFFSET_MISMATCH` | `current_offset`, `expected_total_size` |
| `400 CHUNK_EXCEEDS_EXPECTED_SIZE` | `current_offset`, `expected_total_size` |
| `413 CHUNK_TOO_LARGE` | `max_chunk_bytes` |
| `409 INCOMPLETE_UPLOAD` (finalize) | `current_offset`, `expected_total_size` |

These are additive; every existing field and status code is unchanged.

Session-state rejections (all `409` except `SESSION_EXPIRED`, which can also
be produced lazily the moment TTL passes even before the cleanup CLI runs):
`SESSION_EXPIRED`, `SESSION_CANCELLED`, `SESSION_ALREADY_COMPLETED`,
`SESSION_ALREADY_ASSEMBLED`, `SESSION_FINALIZING`, `SESSION_FAILED`.

Success: `200`

```json
{"success": true, "current_offset": 4194304, "expected_total_size": 99000000}
```

**Resuming after a disconnect**: a client that loses its connection mid-upload
should call route 3 (GET status) to learn the authoritative `current_offset`,
then resume sending chunks starting exactly there. It should never assume its
own last-sent offset is what the server actually has.

### 3. Query session status

`GET /api/uploads/sessions/<id>`

Success: `200`, same `session` object shape as route 1's response
(never includes a raw filesystem path). `404 NOT_FOUND` if missing or not
owned by the caller.

Derived recovery fields:

- `uploaded_bytes`, `remaining_bytes`, `progress_percent`: server-authoritative
  progress; clients should prefer these over local counters after refresh or
  network failure.
- `can_upload_chunks`: true only while the session is active and still missing
  bytes.
- `can_finalize`: true when the session is ready for finalize, including the
  `assembled` queue-retry state.
- `can_retry_finalize`: true only for `assembled`, meaning Project/Pair and
  quota already exist and the client should retry finalize to enqueue only.
- `can_cancel`: true only for `active`.
- `is_terminal`: true for `completed`, `cancelled`, `expired`, or `failed`.
- `pair`: safe pair processing summary once a pair exists; no filesystem paths.
- `processing_job`: safe latest processing-job summary once a job exists.

### 4. Finalize

`POST /api/uploads/sessions/<id>/finalize`

Preconditions: session must be `active` with `current_offset ==
expected_total_size`, OR `assembled` (the enqueue-retry path, see below).
Guarded by a single atomic conditional `UPDATE ... WHERE id=? AND status=?
[AND current_offset=expected_total_size]` - the same pattern already used
elsewhere in this codebase for quota reservation and payment activation. A
second finalize call on an already-`completed` session can never re-run this
work; it gets `409 ALREADY_FINALIZED`.

Conflict responses (`409`, no work performed): `INCOMPLETE_UPLOAD` (still
`active` but bytes don't match), `ALREADY_FINALIZED`, `FINALIZE_IN_PROGRESS`,
`SESSION_CANCELLED`, `SESSION_EXPIRED`, `SESSION_FAILED`,
`SESSION_ASSEMBLED_RETRY` (should not normally surface - see below).

On the winning transition, in order:
1. Recompute the sha256 of the assembled stream and compare against
   `client_checksum_sha256` if one was declared at creation -> `422
   CHECKSUM_MISMATCH` on mismatch.
2. Split the stream at `image_size` and run the image half through the
   existing `validate_image()` and the video half through the existing
   `validate_video()` - the SAME validators (magic-byte + decode checks) the
   normal `/upload` route uses. Failure -> `422 IMAGE_VALIDATION_FAILED` or
   `422 VIDEO_VALIDATION_FAILED`. On any validation failure: the temp file is
   deleted, the session is marked `failed`, and **no Project/ProjectPair is
   ever created and no quota is consumed.**
3. Quota: for a user-owned session, the exact same
   `_reserve_project_quota_atomic()` call the non-resumable `/upload` route
   uses is invoked here, at this exact point (after validation succeeds, before
   the Project row is created) - never earlier, never twice. Admin-owned
   sessions never consume quota (matching the non-resumable Admin upload
   route, which has no quota concept at all). Failure -> `403
   PROJECT_LIMIT_REACHED`, session marked `failed`, no Project/Pair created.
4. Create the `Project` + single `ProjectPair` row, move the two validated
   files into their permanent location via `os.replace()` (atomic rename -
   the same convention the non-resumable path uses; never copy-then-delete),
   generate the QR code via the same helpers the non-resumable path calls.
5. Enqueue processing via the existing RQ mechanism
   (`enqueue_project_pair_processing` / `_schedule_project_pair_processing`) -
   **exactly once**.
   - Success: session -> `completed`, response `200` with the final
     `session` object (`project_id`/`pair_id` populated).
   - **Enqueue itself fails/throws**: the endpoint does **not** report
     success. The session is left in status **`assembled`** with
     `failure_code = "QUEUE_ENQUEUE_FAILED"` - Project/ProjectPair already
     exist, quota is already consumed (this is intentional and mirrors the
     non-resumable path's own behavior when its enqueue attempt fails: it
     also keeps the already-created rows rather than un-creating them).
     Response: `502 QUEUE_ENQUEUE_FAILED`.
   - **Recovery**: calling finalize again on the same session id while it is
     `assembled` retries **only** the enqueue step (no re-validation, no
     second quota consumption, no duplicate Project/Pair). This is the
     documented operator/client recovery path for this failure mode - there
     is no separate CLI for it.

### 4b. Finalize a multi-content-set project

`POST /api/uploads/projects/finalize`

Request: `{"session_ids": [12, 13, 14]}` - ordered, no duplicates, at most 100
entries. Every id must exist and be owned by the caller (otherwise `404
NOT_FOUND`, never a 403, exactly as elsewhere). Every set must agree on
`project_name`, `experience_type`, `playback_mode` and `purpose`
(`409 CONTENT_SET_MISMATCH` otherwise).

Success `200`: `{"success": true, "session": {...first set...}, "sessions":
[per-set summaries]}`. One Project, N ProjectPairs at `pair_index` 0..N-1 in
request order, one project-quota unit, N pair slots, the summed storage bytes
reserved once, 2N `MediaObject` ledger rows, one QR image, one processing job.

| Response | Meaning | Client should |
|---|---|---|
| `200` + `recovered_existing_completion: true` | This exact group already produced a project. | Treat as success. Never retry. |
| `409 INCOMPLETE_UPLOAD` | At least one set is short. `sessions[]` carries every set's authoritative `current_offset` and `set_state`. | Resume ONLY the short sets, then finalize again. |
| `409 FINALIZE_IN_PROGRESS` | Another request holds the group, or a previous attempt settled part of it. | Re-read state; do not start over. |
| `409 SESSION_EXPIRED` | A set passed its inactivity deadline. | Create a fresh session for that set only. |
| `409 CONTENT_SET_MISMATCH` | The sets do not describe one project. | Client bug - clear local state. |
| `403 PAIR_LIMIT_REACHED` | More sets than the plan allows. Refused BEFORE the claim, so every set stays `active` with its bytes. | Reduce sets or upgrade, then finalize again. |
| `422 IMAGE_VALIDATION_FAILED` / `VIDEO_VALIDATION_FAILED` | ONE set's content was rejected. Body carries `failed_session_id` and `failed_set_index`. | Replace that one file, create one new session, finalize the new group. |
| `502 QUEUE_ENQUEUE_FAILED` | Project exists, queue handoff failed; every set parked `assembled`. | Call this route again with the same ids - retries ONLY the enqueue. |

**Failure isolation.** On a per-set validation failure only the offender is
marked `failed` and only the offender's assembled temp file is deleted. Every
sibling is handed back its `active` state with its confirmed bytes untouched, so
a creator whose third video is rejected replaces one file rather than
re-uploading the first two. Failures that happen after the assembled temp files
have been consumed by validation (quota, storage allowance, project creation)
are terminal for the whole group - at that point there are no confirmed bytes
left to preserve for anyone, and saying otherwise would be a lie.

### 5. Cancel

`POST /api/uploads/sessions/<id>/cancel`

Only valid from `active` (documented choice: once a session reaches
`assembled`/`finalizing` it has already consumed quota and created a
Project/Pair - the correct recovery for those states is retrying finalize,
not cancelling). Guarded by the same atomic conditional UPDATE pattern.
Deletes the session's temp file. Releases no quota (none was ever consumed
for a non-finalized session).

Success: `200` with the updated `session` object. Any other current status ->
`409` with one of: `ALREADY_FINALIZED`, `SESSION_ASSEMBLED_RETRY`,
`FINALIZE_IN_PROGRESS`, `SESSION_EXPIRED`, `SESSION_FAILED`, or (already
cancelled) an idempotent-ish `409` as well - cancel is not itself idempotent
past the first call, only chunk retries are.

## Error code vocabulary (all routes)

Every error response has the shape `{"success": false, "code": "...",
"error": "human-readable safe message"}` - never a raw filesystem path, stack
trace, or secret.

`UNAUTHENTICATED`, `NOT_FOUND`, `INVALID_SIZE`, `IMAGE_TOO_LARGE`,
`VIDEO_TOO_LARGE`, `TOTAL_TOO_LARGE`, `INVALID_CHECKSUM`, `ACCOUNT_BLOCKED`,
`PROJECT_LIMIT_REACHED`, `PLAN_NOT_CONFIGURED`, `SUBSCRIPTION_LIMIT`,
`INVALID_OFFSET`, `EMPTY_CHUNK`, `CHUNK_TOO_LARGE`,
`CHUNK_EXCEEDS_EXPECTED_SIZE`, `OFFSET_MISMATCH`, `STORAGE_INCONSISTENT`, `SESSION_EXPIRED`,
`SESSION_CANCELLED`, `SESSION_ALREADY_COMPLETED`, `SESSION_ALREADY_ASSEMBLED`,
`SESSION_FINALIZING`, `SESSION_FAILED`, `SESSION_NOT_ACTIVE`,
`INCOMPLETE_UPLOAD`, `ALREADY_FINALIZED`, `FINALIZE_IN_PROGRESS`,
`SESSION_ASSEMBLED_RETRY`, `CHECKSUM_MISMATCH`, `IMAGE_VALIDATION_FAILED`,
`VIDEO_VALIDATION_FAILED`, `PROJECT_CREATION_FAILED`, `QUEUE_ENQUEUE_FAILED`.

V1.1 Phase 2 additions: `INVALID_PURPOSE`, `INVALID_SESSION_IDS`,
`DUPLICATE_SESSION_IDS`, `TOO_MANY_CONTENT_SETS`, `CONTENT_SET_MISMATCH`,
`GROUP_FINALIZE_REQUIRED`, `PAIR_LIMIT_REACHED`, `STORAGE_LIMIT_REACHED`.

## Retry behavior summary

| Situation | Client should |
|---|---|
| Chunk request timed out / connection dropped | Prefer re-sending the same chunk at the same offset: that is idempotent by contract, so one request answers both "did you get it?" and "here it is again". A `GET` status first also works but costs a round-trip the weak link cannot spare. |
| Got `OFFSET_MISMATCH` | Resume from the `current_offset` in the rejection body. No status read needed. |
| Got `CHUNK_TOO_LARGE` | Re-slice to at most the `max_chunk_bytes` in the rejection body and send again from the same offset. |
| Repeated transport failures | Back off with bounds and jitter, then **pause** - do not cancel the session. Every confirmed byte stays confirmed and a later resume continues from `current_offset`. |
| Got `SESSION_EXPIRED`/`SESSION_CANCELLED`/`SESSION_FAILED` | Start a new session; the old one cannot be revived. |
| Finalize returned `502 QUEUE_ENQUEUE_FAILED` | Call finalize again on the same session id later. |
| Finalize returned `409 INCOMPLETE_UPLOAD` | Resume chunk upload from `current_offset`, then finalize again. For a group, `sessions[]` says which sets are short - resume only those. |
| Group finalize rejected ONE set's content (`422` with `failed_set_index`) | Replace that one file, create one new session for it, finalize the group again with the new id. Never re-upload the sets that passed. |
| A set finished long before its siblings | `GET` its status occasionally. That read slides its inactivity deadline, which is what keeps an early-finishing set alive through a multi-hour group upload. |

## Pause lifetime and cleanup (V1.1 Phase 2)

Two knobs, and it matters which one binds. **Both are inactivity-based, so the
SHORTER of the two is what actually bounds how long a paused upload survives.**

| Setting | Default | Meaning |
|---|---|---|
| `SCANSTORY_UPLOAD_SESSION_TTL_MINUTES` | `1440` (24 h) | The inactivity deadline (`expires_at`). Slid forward by every accepted chunk AND by every owner status read. |
| `SCANSTORY_UPLOAD_SESSION_ABANDONED_STALE_MINUTES` | `1440` (24 h) | The `cleanup-upload-sessions` staleness window, keyed off `updated_at`. Only ever touches `status='active'` rows, bounded by `--limit`. |

Phase 1 shipped the stale window at `120`, which silently undercut the
advertised 24-hour TTL by a factor of twelve: a creator told "resume when you
are ready" lost their bytes after two hours. The default now matches the TTL, so
the advertised window is the real one. An operator under temp-storage pressure
can still set it lower deliberately.

**A status `GET` is a liveness touch.** An owner reading their own `active`
session slides its deadline. This is what keeps a content set that finished its
bytes early alive while its siblings are still crawling: in a three-set upload
on a very weak link, set 1 goes quiet for hours through no fault of its own, and
reaping it while the group is actively progressing would destroy bytes the
creator already paid for in time. An already-expired session is never
resurrected by a read.

**Quota and storage during a pause.** A paused, non-finalized session holds
bytes only in `TMP_UPLOADS_DIR`. It consumes **no** project quota, **no**
account storage allowance, and has **no** `MediaObject` ledger row - all three
are taken at finalize, atomically, for the whole project. That is what makes a
longer recoverable-pause window safe to offer. The operational cost is real
though: a 24-hour window means up to a day of unaccounted temp bytes per
abandoned upload, so `cleanup-upload-sessions --apply` must actually be
scheduled. It is dry-run by default and does nothing if nobody runs it.

## Known limitations (see also the delivery reports)

- No marker-crop metadata support (the non-resumable path's `marker_*`
  fields on `ProjectPair`) - a resumable-created pair always uses
  `marker_mode="full_image"` (the model default). Now that every JS-driven
  creation is resumable, this affects multi-set projects too. The fields are
  diagnostic/provenance only (see `rebuild_pair_features(...,
  apply_legacy_roi=True)`); the uploaded pixels are always the authoritative
  marker. Closing it means either a migration adding `marker_*` to
  `upload_sessions`, or accepting the metadata in the group-finalize body.
- No "attach a resumable content set to an existing project" mode.
- A crash while a session is stuck in `finalizing` is not swept by the
  cleanup CLI (which only ever touches `active` rows) - this is a known,
  documented gap for a future wave, not a silent one.
