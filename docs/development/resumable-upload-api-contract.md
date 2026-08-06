# Resumable Upload API Contract (V1 Wave 5)

Backend-only contract. There is no frontend implementation yet - this document
is written so a future frontend implementer can build against it without
reading `app.py`.

## Scope

One `UploadSession` = one new **single-pair** Project: exactly one image and
one video, uploaded as a single sequential byte stream. The client sends the
image's bytes first (`image_size` bytes), then immediately the video's bytes
(`video_size` bytes) - back to back, no separator. `expected_total_size =
image_size + video_size`. There is no support in this wave for multi-pair
resumable projects or attaching a resumable pair to an existing project - a
brand-new project is always created on successful finalize.

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
    "image_size": 123456,
    "video_size": 98765432,
    "project_id": null,
    "pair_id": null,
    "failure_code": null,
    "created_at": "...", "updated_at": "...", "expires_at": "...", "completed_at": null
  }
}
```

`expires_at` is `created_at + 1440 minutes` (24h) by default
(`SCANSTORY_UPLOAD_SESSION_TTL_MINUTES`) - generous on purpose: large video
files over slow/mobile networks may legitimately take a long time to fully
upload in chunks. Compare the payment-reservation TTL (30 min) which gates a
much shorter checkout flow; a resumable upload's TTL is deliberately a
different order of magnitude for a different reason (slow transfer, not
checkout abandonment).

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
  accepted bytes): `409 OFFSET_MISMATCH`.
- Empty body: `400 EMPTY_CHUNK`.

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
`INVALID_OFFSET`, `EMPTY_CHUNK`, `CHUNK_EXCEEDS_EXPECTED_SIZE`,
`OFFSET_MISMATCH`, `STORAGE_INCONSISTENT`, `SESSION_EXPIRED`,
`SESSION_CANCELLED`, `SESSION_ALREADY_COMPLETED`, `SESSION_ALREADY_ASSEMBLED`,
`SESSION_FINALIZING`, `SESSION_FAILED`, `SESSION_NOT_ACTIVE`,
`INCOMPLETE_UPLOAD`, `ALREADY_FINALIZED`, `FINALIZE_IN_PROGRESS`,
`SESSION_ASSEMBLED_RETRY`, `CHECKSUM_MISMATCH`, `IMAGE_VALIDATION_FAILED`,
`VIDEO_VALIDATION_FAILED`, `PROJECT_CREATION_FAILED`, `QUEUE_ENQUEUE_FAILED`.

## Retry behavior summary

| Situation | Client should |
|---|---|
| Chunk request timed out / connection dropped | Re-send the same chunk at the same offset it tried before, or `GET` status first to confirm `current_offset`, then resume from there. |
| Got `OFFSET_MISMATCH` | `GET` status, resume from the returned `current_offset`. |
| Got `SESSION_EXPIRED`/`SESSION_CANCELLED`/`SESSION_FAILED` | Start a new session; the old one cannot be revived. |
| Finalize returned `502 QUEUE_ENQUEUE_FAILED` | Call finalize again on the same session id later. |
| Finalize returned `409 INCOMPLETE_UPLOAD` | Resume chunk upload from `current_offset`, then finalize again. |

## Known V1 limitations (see also the final delivery report)

- No marker-crop metadata support (the non-resumable path's `marker_*`
  fields on `ProjectPair`) - a resumable-created pair always uses
  `marker_mode="full_image"` (the model default).
- No multi-pair resumable projects; no "attach to an existing project" mode.
- A crash while a session is stuck in `finalizing` is not swept by the
  cleanup CLI (which only ever touches `active` rows) - this is a known,
  documented gap for a future wave, not a silent one.
