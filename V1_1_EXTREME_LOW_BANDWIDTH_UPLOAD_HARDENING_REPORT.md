# V1.1 — Extreme Low Bandwidth Upload Hardening

Branch `agent/v1.1-platform-admin`, worktree `F:\ScanStory-main\ScanStory-v1.1-agent1`.

The whole pass serves one sentence: **every byte the server has confirmed stays
confirmed.** Slow is acceptable. Restarting from zero after a recoverable
failure is not.

The short version of what was found: the *server* side of this was already in
very good shape — server-authoritative offsets, idempotent duplicate chunks,
an atomic conditional-UPDATE gate against double finalization, atomic file
promotion, RQ separation, structured timing telemetry. The *client* side was
where the promise was being broken. It sent a fixed 1 MiB chunk regardless of
the link, retried in a tight loop with no delay, recognised only three error
shapes as recoverable, and ended a network failure by telling the creator
"Upload failed" — while every uploaded byte sat safe on the server. Refresh
recovery had been written but could never actually fire because of a
two-field bug in what it saved.

---

## 1. Starting HEAD

`b2ac7f33ac9ef2b6eeac98ce20f90c46a8ae4019` — *Merge branch
'agent/v1.1-experience-ux' into develop/scanstory-v1.1*.

Verified before starting: branch `agent/v1.1-platform-admin`, working tree
clean, HEAD equal to the authoritative integration HEAD.

`df5e8f1` (*fix(v1.1): close release processing and CSP blockers*) was read
first, as instructed. It touched `app.py`, `processing_queue.py` and two test
files, and its subject matter was the **reprocess/fix** path's processing
idempotency plus a CSP blocker for the scanner — not the upload-finalize path.
Its processing-idempotency behaviour is preserved untouched here; scenario 23
below asserts the finalize path's enqueue-exactly-once behaviour on top of it
rather than replacing it.

## 2. Ending HEAD

The last commit of the change set is `17c69dd` — *Measure the low-bandwidth
claims instead of asserting them*. On top of it sits one further commit,
*Report the low-bandwidth upload hardening pass*, which adds this document
and nothing else; its hash is deliberately not quoted here, because a commit
cannot contain its own hash and quoting a guess would be worse than pointing
at it. `git rev-parse HEAD` on `agent/v1.1-platform-admin` is the
authoritative answer, and `git diff 17c69dd..HEAD --stat` shows that the
difference is this file alone.

## 3. Commits

| Hash | Subject |
|---|---|
| `fb09801` | Let a weak-link client recover a chunk rejection in one round-trip |
| `28236bb` | Keep a dropped upload paused and resumable instead of failed |
| `ffd6a68` | Add the 23 low-bandwidth chaos scenarios |
| `ff5622b` | Re-point three shape guards at the new uploader, keep the copy guard honest |
| `17c69dd` | Measure the low-bandwidth claims instead of asserting them |
| *(HEAD)* | Report the low-bandwidth upload hardening pass — this document, no code |

## 4. Files changed

`git diff --stat b2ac7f3..HEAD` (excluding this report):

| File | Change |
|---|---|
| `app.py` | +50 / −7 — recoverable-rejection payloads, `max_chunk_bytes`, sliding inactivity deadline |
| `templates/user/user_create_project.html` | +427 / −45 — adaptive chunking, retry classification, pause/resume, fingerprinting, persistence |
| `tests/integration/test_extreme_low_bandwidth_upload.py` | +793 — new, the 23 chaos scenarios plus 3 adaptive-sizing tests |
| `tests/gate_jr/test_marker_selection_upload.py` | +22 / −5 — shape guards re-pointed |
| `tests/gate_jr/test_v11_experience_ux.py` | +10 / −4 — resume-matcher guard re-pointed |
| `docs/development/resumable-upload-api-contract.md` | +40 / −4 — additive fields, inactivity semantics, corrected retry guidance |
| `scripts/dev/low_bandwidth_upload_server.py` | +77 — new, throwaway isolated instance for certification |
| `scripts/dev/low_bandwidth_upload_certification.mjs` | +487 — new, CDP throttled-network driver |
| `evidence/low_bandwidth/throttled_upload_certification.json` | +256 — measured results |

No migration. No new dependency. No file outside this worktree touched.

## 5. Existing upload architecture map

Read in full before anything was changed. It was already a coherent design,
and the mandate to preserve rather than rewrite it was the right one.

**Model** — `models.UploadSession` (`upload_sessions`), lines 2211–2323 of
`models.py`. One session = one new single-pair Project, uploaded as a single
sequential byte stream: image bytes first, then video bytes, split at
`image_size`. Fields that carry the contract: `current_offset`,
`expected_total_size`, `image_size`, `video_size`, `status`, `storage_token`
(server-generated UUID4 — never client input, never derived from a filename),
`expires_at`, `failure_code`, `client_checksum_sha256`. Two CHECK constraints
already enforce `0 <= current_offset <= expected_total_size` at the database
level. `status` enum: `active` → `finalizing` → (`assembled` → `completed`) |
`failed` | `cancelled` | `expired`.

**Routes** — all in `app.py`, section beginning line 8984:

| Route | Function | Line |
|---|---|---|
| `POST /api/uploads/sessions` | `create_upload_session` | 9265 |
| `POST /api/uploads/sessions/<id>/chunk` | `upload_session_chunk` | 9385 |
| `GET /api/uploads/sessions/<id>` | `upload_session_status` | 9530 |
| `POST /api/uploads/sessions/<id>/finalize` | `finalize_upload_session` | 9915 |
| `POST /api/uploads/sessions/<id>/cancel` | `cancel_upload_session` | 10005 |
| `flask cleanup-upload-sessions` | CLI, bounded batch | 10034 |

**Chunk semantics as found** — `X-Chunk-Offset` header plus a raw body. Three
branches under a row lock (`_lock_upload_session`, `with_for_update()` where
the backend supports it): exact-offset match appends and advances;
fully-contained replay (`claimed < current` **and** `claimed + len <=
current`) returns `200` with `"note": "duplicate_chunk_ignored"`; anything
else returns `409 OFFSET_MISMATCH`. There is also a self-heal for a crash
between file-append and DB-commit: a temp file *ahead* of `current_offset` is
truncated back to it, and a file *behind* it is a hard
`500 STORAGE_INCONSISTENT`.

**Finalize semantics as found** — an atomic conditional `UPDATE ... WHERE
status='active' AND current_offset=expected_total_size` claims the right to
finalize; a loser gets a `409` conflict code. `_finalize_assemble_and_validate`
then mirrors `handle_upload()` step for step: optional whole-file checksum,
`validate_image`/`validate_video` over `_BoundedFileView` slices (no redundant
on-disk copy), `_storage.reserve_account_storage`,
`_reserve_project_quota_atomic`, Project + ProjectPair creation,
`os.replace()` atomic promotion, QR generation, then exactly one
`_schedule_project_pair_processing()`. An enqueue failure parks the session in
`assembled`, and re-calling finalize retries **only** the enqueue.

**Client as found** — inline in `templates/user/user_create_project.html`
(single-pair path only; multi-pair still uses the legacy XHR uploader).
`submitResumableSinglePair` → `uploadResumableStream` → `finalizeResumableWithBoundedRetry`,
with `localStorage` under `scanstory.resumableUpload.v1` for refresh recovery.

**Tests as found** — `tests/integration/test_resumable_upload.py` (46 tests)
and `tests/integration/test_upload_edge_hardening.py` (2). Both were read
before writing anything new, and the new file follows their established
patterns: real decodable media via `_jpeg_bytes()`/`_mp4_bytes()`, locally
duplicated fixtures (the documented workaround for this environment's stray
global `tests` package shadowing dotted imports), and `_patch_qr` for
finalize paths.

**Two conclusions from the audit that shaped everything after it.** First,
most P0 *server* requirements were already met, so the honest work was
narrow: hand a rejected client the data it needs to recover, and stop the
wall clock from outliving a paused upload. Second, the P0 *client*
requirements were essentially unimplemented, and that is where the pass spent
its weight.

## 6. Current chunk size behaviour (as found)

Server ceiling: `SCANSTORY_RESUMABLE_CHUNK_MAX_BYTES`, default `1024 * 1024`
(1 MiB), validated positive at import and published into
`app.config["RESUMABLE_UPLOAD_CHUNK_MAX_BYTES"]`. Enforced twice per request —
once on `Content-Length`, once on the actual body length.

Client: `const RESUMABLE_UPLOAD_CHUNK_SIZE = 1024 * 1024;` — a hardcoded
constant, identical for a 5 Mbps office link and a 0.15 Mbps train, and a
hardcoded *duplicate* of the server default with nothing keeping the two in
step. Lowering the server config would have started returning `413` to a
client that had no way to know why.

At 0.15 Mbps a 1 MiB chunk takes roughly 56 seconds. Losing the connection at
55 seconds threw away 55 seconds of work.

## 7. Adaptive chunk implementation

An EWMA of measured chunk throughput, targeting roughly 8 seconds per chunk,
clamped to the server's declared ceiling.

```
RESUMABLE_CHUNK_MIN_BYTES      = 128 KB
RESUMABLE_CHUNK_MAX_BYTES      = 5 MB     (hard client maximum)
RESUMABLE_CHUNK_STEP_BYTES     = 64 KB    (quantisation)
RESUMABLE_CHUNK_TARGET_SECONDS = 8
RESUMABLE_THROUGHPUT_SMOOTHING = 0.25     (newest-sample weight)
RESUMABLE_CHUNK_RESIZE_RATIO   = 1.5      (hysteresis)
```

- **Ceiling** is read from `session.max_chunk_bytes`, newly published by the
  server. `resumableChunkCap()` takes `min(server ceiling, 5 MB)` and floors
  at 128 KB, so the client can never 413 itself and can never be talked into
  an absurd chunk either.
- **Seed** — `initialChunkBytes()` uses `navigator.connection.effectiveType`
  as a hint for the *first* chunk only: `slow-2g`/`2g` → 128 KB, `3g` →
  256 KB, otherwise 512 KB. From the second chunk on, the wire is the
  authority. The hint routinely disagrees with what an uplink actually does,
  which is exactly why it is not trusted beyond chunk one.
- **Resize** — `nextChunkBytes()` computes `smoothed × 8 s`, quantises to
  64 KB, and only acts if the target differs from the current size by ≥ 1.5×.
  Growth is capped at a doubling per step; shrinking goes straight to target,
  because a collapsing link needs the small chunk now, not three chunks from
  now.
- **Duplicates never feed the estimator.** A `duplicate_chunk_ignored`
  response means the server discarded a replay; it says nothing about link
  speed, and letting it in would poison the average.
- **Failure shrink** — two consecutive transport failures at the same offset
  halve the chunk. A link that cannot carry this chunk will not carry it on
  the third attempt.
- **No parallelism, ever.** One chunk in flight. Concurrency on a weak mobile
  link buys nothing and costs a retry storm.

Observed bands: a 20 KB/s link settles at 192 KB (~10 s/chunk, inside the
128–256 KB "very poor" band); sustained speed climbs by doublings and stops
at the server ceiling; a 20% measurement wobble changes nothing.

## 8. Server-authoritative resume behaviour

The server was already the source of truth and remains so. `GET
/api/uploads/sessions/<id>` returns `current_offset`, `expected_total_size`,
`status`, `is_terminal`, `can_upload_chunks`, `can_finalize`,
`can_retry_finalize`, `failure_code`, `project_id`/`pair_id`, and (new)
`max_chunk_bytes`, with `Cache-Control: no-store`.

What changed is that the client now *behaves* like the server is
authoritative in every path, not most of them:

- `uploadResumableStream` starts from `session.current_offset` — the value the
  server just returned — never from a stored local counter. The stored
  `currentOffset` is explicitly commented as UI bookkeeping only.
- On resume after refresh, the flow is: read saved metadata → prove file
  identity → `GET` session state → start from the server's offset. The saved
  offset is never read for control flow.
- A disagreement in *either* direction resolves to the server's value: a
  client that thinks it is ahead gets `409` with the true offset inline; a
  client that thinks it is behind replays harmlessly and is told the true
  offset in the `200`.

Scenario 7 asserts both directions converge on the same server-held truth,
and that the assembled file on disk matches.

## 9. Chunk idempotency behaviour

Unchanged where it was already right; completed where it was thin.

- **Exact replay** (fully contained in already-accepted bytes) → `200`,
  `"note": "duplicate_chunk_ignored"`, unchanged offset, nothing appended.
  This is what makes the client's "just re-send the same chunk" retry safe.
- **Partial overlap** (`claimed < current` but `claimed + len > current`) →
  `409 OFFSET_MISMATCH`, **deliberately not spliced**. Splicing would mean
  trusting that the overlapping prefix is byte-identical to what is already
  on disk, and nothing in this protocol proves that. Rejecting preserves the
  invariant; the client recovers in one round-trip because the authoritative
  offset now travels in the rejection body.
- **Gap / future offset** → `409 OFFSET_MISMATCH`, no hole created.
- **Malformed offset** (absent, negative, non-integer, empty string) → `400
  INVALID_OFFSET`. **Empty body** → `400 EMPTY_CHUNK`. **Over-declared-size**
  → `400 CHUNK_EXCEEDS_EXPECTED_SIZE`. **Over-ceiling** → `413
  CHUNK_TOO_LARGE`, now carrying `max_chunk_bytes`.
- Not one rejection advances the offset or writes a byte — asserted against
  the on-disk file, not just against `current_offset`.

New in this pass: `OFFSET_MISMATCH`, `CHUNK_EXCEEDS_EXPECTED_SIZE` and
`INCOMPLETE_UPLOAD` carry `current_offset` + `expected_total_size`;
`CHUNK_TOO_LARGE` carries `max_chunk_bytes`. These are additive — every
existing status code, error code and success field is unchanged. The reason
is bandwidth, not elegance: on a very weak uplink an extra round-trip per
recoverable rejection is real dead time, and the server already knew the
answer when it said no.

## 10. Finalize idempotency behaviour

Verified against `df5e8f1` first, as instructed. That commit hardened the
**reprocess/fix** path; the upload-finalize path's idempotency was already
established earlier and is untouched here. No change was made to finalize —
the audit found no genuine remaining gap, and inventing one would have risked
the very behaviour `df5e8f1` established.

Confirmed by execution rather than by reading:

- Finalize ×3 → exactly one Project, one ProjectPair, one ProcessingJob; the
  second and third get `409 ALREADY_FINALIZED` (scenario 8).
- A lost finalize response recovers from `GET` status reporting
  `completed` + `project_id` + `is_terminal` (scenario 9).
- `_schedule_project_pair_processing` is called exactly once across three
  finalize calls (scenario 10).
- A replay storm during transfer plus three finalize calls still yields one
  Project and exactly two `MediaObject` ledger rows — one `trigger_image`,
  one `video` (scenario 22).
- The nastiest recovery path: first enqueue fails → `502
  QUEUE_ENQUEUE_FAILED`, session parks in `assembled`, zero jobs; retry
  finalize → exactly one job; two further replays add nothing, and the single
  pair is untouched (scenario 23).

## 11. Retry / backoff behaviour

Failures are now classified, in `uploadRetryDecision(err, attempt)`, which
returns one of five actions. Codes are checked **before** status, because a
`500 STORAGE_INCONSISTENT` is not worth retrying however retryable its status
class looks, and a `413 CHUNK_TOO_LARGE` is fixed by sending less rather than
by sending the same thing again.

| Action | Triggered by | Behaviour |
|---|---|---|
| `resync` | `OFFSET_MISMATCH` | Take the offset from the rejection body, re-slice, continue. No wait. |
| `shrink` | `CHUNK_TOO_LARGE` | Halve toward the advertised ceiling, resend from the same offset. No wait. |
| `stop` | 23 terminal codes; bare 400/401/403; any non-retryable status | Surface a safe message. No retry. |
| `retry` | transport death (status 0 / `TypeError`), 408, 429, 5xx | Bounded backoff, then re-send the same bytes at the same offset. |
| `pause` | retry budget spent | See §12. |

Backoff: `1s / 2s / 4s / 8s / 15s / 30s`, each with jitter drawn from
`[0, min(base, 1000))` so a fleet of phones reconnecting together does not
arrive in lockstep. `Retry-After` is honoured when the server sends one —
both delta-seconds and HTTP-date forms — clamped to 60 s so a hostile or
mistaken value cannot park an upload for an hour. Absent or unparseable
header falls back to the table.

Terminal codes: `UNAUTHENTICATED`, `ACCOUNT_BLOCKED`, `NOT_FOUND`,
`PROJECT_LIMIT_REACHED`, `STORAGE_LIMIT_REACHED`, `PLAN_NOT_CONFIGURED`,
`SUBSCRIPTION_LIMIT`, `INVALID_SIZE`, `IMAGE_TOO_LARGE`, `VIDEO_TOO_LARGE`,
`TOTAL_TOO_LARGE`, `INVALID_CHECKSUM`, `CHECKSUM_MISMATCH`, `INVALID_OFFSET`,
`EMPTY_CHUNK`, `CHUNK_EXCEEDS_EXPECTED_SIZE`, `INVALID_EXPERIENCE_PLAYBACK`,
`IMAGE_VALIDATION_FAILED`, `VIDEO_VALIDATION_FAILED`, `SESSION_EXPIRED`,
`SESSION_CANCELLED`, `SESSION_FAILED`, `STORAGE_INCONSISTENT`.

One deliberate design choice worth naming: a transport retry **re-sends the
same chunk at the same offset** rather than asking the server where it got to
first. That is idempotent by contract, so a single request answers both "did
you receive it?" and "here it is again" — and on a weak uplink the saved
round-trip is real time. If the server did receive it, the reply is a
duplicate-ignored carrying the true offset.

Note on 429: the resumable endpoints are **not** rate-limited server-side,
and deliberately were not given a limiter here — a low-bandwidth upload is
inherently many small requests, and throttling it would be actively harmful.
The 429 handling exists because a reverse proxy or CDN in front of the app
can legitimately emit one, and the client must not treat that as fatal.

## 12. Pause / resume behaviour

Paused is a resting state, not a failure state. When the bounded automatic
attempts are spent, `uploadResumableStream` throws
`UPLOAD_PAUSED_NETWORK` and the handler:

1. leaves the `UploadSession` `active` — no cancel call, no destruction;
2. leaves the saved local state in place (explicitly *not* cleared);
3. shows *"Your connection dropped. Your uploaded progress is safe."* with
   the exact byte count already uploaded;
4. calls `pauseUploadForNetwork()`, which re-enables the form and relabels
   the submit button **"Resume upload"**.

Resume needs no new machinery: a second submit reads the saved session,
proves file identity, asks the server for its confirmed offset, and continues.
That *is* the resume path, which is why it was reused rather than duplicated.

Auto-resume: an `online` listener resumes a paused upload without the creator
watching for it, via `form.requestSubmit()`, bounded to 3 automatic resumes
per page so a flapping connection cannot loop.

State mapping onto what already existed, rather than new DB enum values:

| Conceptual | Server `status` | Client `data-upload-state` |
|---|---|---|
| UPLOADING | `active` | `uploading` / `slow` |
| PAUSED_NETWORK | `active` (untouched) | `paused` |
| RESUMING | `active` | `resuming` |
| UPLOAD_COMPLETE | `active` at full offset → `finalizing` | `uploaded` |
| PROCESSING | `completed` + RQ job | `processing` |
| READY | `completed`, pair processed | `ready` |

No `UploadSession.status` value was added. The two states the mission names
that the server does not have (`PAUSED_NETWORK`, `RESUMING`) are *client*
states over an untouched `active` session, which is the correct place for
them: a pause is something the browser decided, not something the server
needs to know.

## 13. Refresh recovery behaviour

On load the creator re-picks their files (a page cannot hold File handles
across a reload). The uploader then:

1. reads the saved record;
2. computes fingerprints for the freshly-picked marker and video;
3. compares project name, experience type, playback mode, marker and video
   name/size/lastModified, **and** both fingerprints;
4. on mismatch, logs `RESUMABLE CLIENT RESUME REJECTED` with reason
   `file_identity_mismatch`, clears the record and starts a clean session —
   never appends the new bytes to the old stream;
5. on match, `GET`s the session and branches on the server's `status`:
   `completed` → straight to the success page; `assembled` → retry finalize
   only; `active` → resume from the server's offset; `is_terminal` → clear
   and report safely.

**Two bugs found and fixed here.** `saveResumableUploadState` never recorded
`experience_type` or `playback_mode`, but `storedSessionMatchesFiles`
compared them — so both comparisons hit `undefined` and refresh recovery
could **never** fire. It looked implemented and was dead. Second, no
throughput hints were stored, so a resumed upload re-learned the link speed
from scratch: the first expensive minute of a very weak transfer, paid twice.

Measured: a real Chrome reload mid-transfer resumed from 262 144 B and
finished the remaining bytes with **0 bytes retransmitted** (§28).

## 14. IndexedDB / local persistence behaviour

**localStorage, deliberately, and this is a considered decision rather than a
shortcut.** What must survive a reload is a few hundred bytes of metadata.
localStorage stores exactly that, synchronously, with no schema and no
upgrade path to maintain. IndexedDB would earn its complexity only if a page
could persist the `File` handles themselves — and it cannot, which is why the
fingerprint in §15 has to exist at all. Adding an object store, a version
number and an upgrade callback to hold the same JSON would be complexity with
no behaviour attached to it.

Key `scanstory.resumableUpload.v2` (bumped from `v1` because the record
gained required fields; a `v1` record simply fails the version check and is
ignored). Contents:

| Field | Why |
|---|---|
| `sessionId` | which server session to ask about |
| `projectName`, `experience_type`, `playback_mode` | resume-eligibility (the two that were missing) |
| `imageName/Size`, `videoName/Size`, `videoLastModified` | cheap first-pass identity |
| `imageFingerprint`, `videoFingerprint` | real identity proof |
| `expectedTotalSize` | progress rendering |
| `currentOffset` | **UI bookkeeping only** — never control flow |
| `chunkBytes`, `smoothedBytesPerSecond`, `networkQuality` | don't re-learn the link |
| `retryCount`, `resumeCount`, `pauseCount`, `lastSuccessAt` | telemetry (§22) |

**Nothing sensitive is stored.** No auth token, no CSRF token, no URL that
grants access. The session id alone is useless to another account: every
`/api/uploads/sessions` route re-checks ownership server-side and returns
`404` for a session that is not yours — which is why no client-side owner
check was added. That guard already exists, at the only layer where it can be
trusted, and
`test_foreign_user_cannot_recover_completed_upload_session` already covers it.

## 15. Fingerprint behaviour

`fileFingerprint(file)` returns `{ name, size, lastModified, headSha256,
tailSha256 }` — SHA-256 over the first 64 KB and the last 64 KB via
`crypto.subtle`. Never the whole file: hashing 200 MB on a phone to decide
whether a resume is safe would cost more than the resume saves, and two
blocks are enough to tell two exports of the same clip apart.

`fingerprintsMatch(stored, current)` requires name, size and lastModified to
agree, and any recorded hash to agree. A recorded `null` — a browser without
SubtleCrypto, e.g. a non-secure context — degrades to metadata-only identity
rather than losing the ability to resume at all. A mismatch stops the resume
and starts a clean session; a false negative costs a restart (safe), a false
positive would corrupt media (not safe), so the asymmetry is deliberate.

Both marker and video are fingerprinted. The marker is re-rendered from
canvas on each submit, so in the rare case its bytes differ between renders
the session is simply recreated — the safe outcome.

## 16. Integrity behaviour

No new per-chunk integrity mechanism was added, because the audit found no
gap that one would close.

What already protects the bytes: the sequential-offset contract itself (the
server accepts bytes only at `current_offset`, all-or-nothing under a row
lock); the two database CHECK constraints; the append-then-commit ordering
plus the truncate self-heal for a crash between them; a
`CHUNK_EXCEEDS_EXPECTED_SIZE` guard before any write; a size equality check
at finalize (`os.path.getsize(combined) != expected_total_size` → `500
STORAGE_INCONSISTENT`); and the optional whole-stream
`client_checksum_sha256`, recomputed at finalize in 1 MiB blocks and rejected
on mismatch before any validation, quota or Project work.

A per-chunk digest header would add bytes to every request on precisely the
links that can least afford them, to detect a corruption mode that the
optional whole-file checksum already catches. **Deferred as P1, with the
reasoning recorded rather than the feature added.** Scenarios 1, 5, 21 and 22
assert byte-exactness by reading the assembled temp file off disk, which is
what would actually catch a splicing bug.

## 17. Upload-session expiry behaviour

**Changed: `expires_at` is now an inactivity deadline, not a wall clock
started at session creation.** Every accepted chunk slides it forward by the
full TTL.

The failure this fixes is the pause story in §12. A creator who pauses
overnight on a bad link must still be able to resume the bytes the server has
already confirmed; a wall clock from creation takes those bytes away for no
protocol reason. It also removes any possibility of a genuinely long slow
transfer being killed mid-flight by the clock.

Two knobs, and it matters which one actually binds:

- `SCANSTORY_UPLOAD_SESSION_TTL_MINUTES` (default 1440) — now the inactivity
  deadline.
- `SCANSTORY_UPLOAD_SESSION_ABANDONED_STALE_MINUTES` (default 120) — the
  `cleanup-upload-sessions` staleness window, which only ever touches
  `status='active'` rows and is bounded by `--limit`.

Since both are inactivity-based, **the shorter one is what bounds how long a
paused upload survives**: with defaults, a pause longer than 2 hours is
reclaimed by cleanup (if cleanup is scheduled — it is a manually invoked CLI,
dry-run by default). An operator who wants to support an overnight pause must
raise `SCANSTORY_UPLOAD_SESSION_ABANDONED_STALE_MINUTES`, not the TTL. This is
documented in the contract doc rather than silently assumed, and a reclaimed
session is rejected cleanly with `SESSION_EXPIRED` so the client starts fresh
instead of failing obscurely.

Scenario 19 winds a session's deadline to one minute away, sends a chunk, and
asserts the deadline moved back out and the session stayed `active`.
Scenario 18 asserts an actually-expired session is rejected with
`SESSION_EXPIRED`, transitions to `expired` with
`failure_code=SESSION_TTL_EXPIRED`, and produced no project.

## 18. Atomic finalization result

Already correct; verified, not modified. Temp file under `TMP_UPLOADS_DIR`
named from the server-generated `storage_token` → validate both slices via
`_BoundedFileView` → `validate_image`/`validate_video` copy out to their own
temps → the combined temp is deleted → quota and storage reserved atomically
→ Project + ProjectPair created and flushed → `os.replace()` promotes each
validated file into `data/images` and `data/videos` → `MediaObject` ledger
rows recorded → DB commit → enqueue.

A half-written file is never exposed: `os.replace()` is atomic, and only
already-validated content reaches it. Failure before completion is either
safely recoverable (`assembled` + retry finalize) or terminally explicit
(`failed` + a `failure_code`), with saved paths unlinked and temps removed on
the failure paths. On Windows the view handles are closed in a `finally`
before any delete — otherwise cleanup would silently no-op, which the
existing code already noted.

## 19. Upload / processing separation

Unchanged and correct. Processing never runs inline in an upload request:
finalize calls `_schedule_project_pair_processing()`, which is the same RQ
enqueue the non-resumable `/upload` route uses. Redis/RQ architecture
untouched.

Client-side copy now says so plainly. Once the bytes are across, the
`uploaded` state note reads *"Your upload is complete. Processing continues
automatically."* The creator is not asked to keep the page open for
processing — only for the transfer, and the `slow` note says exactly that
(*"you can leave this tab open"*). The uploader deliberately does **not**
promise that a closed tab keeps uploading, because it would not be true: no
service worker is involved, and adding one was out of scope.

## 20. Lightweight status behaviour

Audited; no new endpoint added, because a suitable one already exists.
`pollProcessingJobIfAvailable()` polls `GET /api/processing/jobs/<id>` — a
small dedicated job-status payload, not a full project fetch. Backoff is
already progressive (1200 ms × 1.6, capped at 6 s, 8 attempts) rather than a
one-second-forever loop, and it surfaces only `queued`/`processing`/
`completed`/`failed` with no internal queue identifiers.

The session payload also carries a compact `pair` summary and
`processing_job` block, with `processing_error` passed through
`safe_error_summary()` so no internal detail leaks. Adding another endpoint
would have duplicated a working one.

## 21. Low-bandwidth UI result

Eleven visible states, all driven off work the uploader was already doing:
`preparing / uploading / slow / retrying / interrupted / resuming / paused /
uploaded / processing / ready / failed`. `paused` is deliberately **not** in
`UPLOAD_TRANSFER_STATES`, so the stall watchdog cannot flag a pause it caused.

Every progress render shows the byte pair (`X of Y`), the percentage, the
smoothed rate and an ETA. The rate and ETA come from the **smoothed**
throughput rather than the average since start, so a link that just recovered
stops quoting the stall it recovered from.

Copy, plain language, no stack traces, no HTTP codes, no queue terminology:

| State | Text |
|---|---|
| `slow` | Slow connection detected. This is taking a while, but the upload is still going — you can leave this tab open. |
| `retrying` | Retrying the last step. Nothing you already uploaded is lost. |
| `interrupted` | Upload interrupted. Waiting for your connection to come back — it will pick up where it stopped. |
| `resuming` | Connection restored. Resuming upload… |
| `paused` | Upload paused. Resume when you're ready — everything already uploaded is safe. |
| `uploaded` | Your upload is complete. Processing continues automatically. |

Plus, in the progress panel itself: *"Your connection dropped. Your uploaded
progress is safe."* on pause, and *"Resuming from the last byte the server
confirmed."* on resync. A recoverable interruption is never described as
"Upload failed — start over"; a genuine terminal failure still says so, and
even then the copy points at the retry affordance rather than at a restart.

"Slow" stays qualitative. No bandwidth number is printed anywhere, and an
existing test guards that — a guard that caught two of my own *comments*
mentioning a speed and was left exactly as strict as it was rather than
weakened.

## 22. Telemetry result

Existing structured server-side `_log_upload_timing` events
(`upload_session_create` / `_chunk` / `_finalize`) are unchanged, including
the field allowlist that keeps anything unlisted out of the log, and the
existing tests that assert no `@`, no `password`/`secret`/`token`, and no
filesystem path appears in a payload.

Client-side structured events, extended:

| Event | Carries |
|---|---|
| `RESUMABLE CLIENT CHUNK ACCEPTED` | offset, total, chunk bytes, network quality, duplicate flag |
| `RESUMABLE CLIENT CHUNK RESIZE` | from/to bytes, measured bytes/s, quality label |
| `RESUMABLE CLIENT CHUNK SHRINK` | to bytes, reason |
| `RESUMABLE CLIENT RETRY` | attempt, wait ms, whether `Retry-After` was honoured, status, reason |
| `RESUMABLE CLIENT RESUME` | authoritative offset, reason |
| `RESUMABLE CLIENT PAUSED` | offset, attempts, reason |
| `RESUMABLE CLIENT RESUME REJECTED` | reason (`file_identity_mismatch`) |
| `UPLOAD CLIENT PROGRESS` | bytes, percentage, smoothed speed, ETA |

Counters persisted alongside the session record: `retryCount`, `resumeCount`,
`pauseCount`, `lastSuccessAt`, `networkQuality`, `chunkBytes`. No file
content, no secret, no auth token, no `DATABASE_URL`, no filename beyond what
the creator already sees on their own screen.

## 23. Focused tests

**New: `tests/integration/test_extreme_low_bandwidth_upload.py` — 26 tests,
26 passed.** One test per numbered chaos scenario, in order, so a failure
names the behaviour that regressed rather than a helper, plus three
adaptive-sizing tests.

All 23 required scenarios are covered, none merged and none skipped:

| # | Scenario | Test | Result |
|---|---|---|---|
| 1 | Normal chunk upload | `test_01_normal_sequential_chunk_upload_assembles_exact_bytes` | pass |
| 2 | Duplicate same chunk | `test_02_duplicate_chunk_is_idempotent_and_appends_nothing` | pass |
| 3 | Stale offset | `test_03_stale_offset_rejected_with_authoritative_offset_inline` | pass |
| 4 | Gap / future offset | `test_04_future_offset_rejected_and_leaves_no_hole` | pass |
| 5 | Lost chunk response then retry | `test_05_lost_chunk_response_then_replay_does_not_duplicate_bytes` | pass |
| 6 | Interrupted then resume | `test_06_interrupted_upload_resumes_from_server_offset` | pass |
| 7 | Browser says X, server says Y | `test_07_server_offset_wins_over_client_belief` | pass |
| 8 | Duplicate finalize | `test_08_triple_finalize_produces_exactly_one_project_pair_and_job` | pass |
| 9 | Finalize after response loss | `test_09_lost_finalize_response_recovers_completion_from_status` | pass |
| 10 | Processing enqueued once | `test_10_processing_enqueued_exactly_once_across_finalize_replays` | pass |
| 11 | 5xx retry | `test_11_server_error_statuses_are_retried_with_bounded_backoff` | pass |
| 12 | Network failure retry | `test_12_transport_failure_is_retryable_not_terminal` | pass |
| 13 | 429 `Retry-After` | `test_13_429_honours_retry_after_seconds_and_http_date` | pass |
| 14 | 401/403 stops retry | `test_14_auth_and_policy_failures_stop_immediately` | pass |
| 15 | Retry exhaustion pauses | `test_15_retry_exhaustion_pauses_rather_than_failing` | pass |
| 16 | Resume after pause | `test_16_resume_after_pause_continues_from_server_offset` | pass |
| 17 | Fingerprint mismatch blocks resume | `test_17_fingerprint_mismatch_blocks_resume` | pass |
| 18 | Expired session safe handling | `test_18_expired_session_is_rejected_safely_not_silently` | pass |
| 19 | Active session doesn't expire early | `test_19_chunk_activity_slides_the_inactivity_deadline` | pass |
| 20 | Malformed chunk / range rejected | `test_20_malformed_offsets_and_ranges_are_rejected` | pass |
| 21 | No duplicate bytes | `test_21_replay_storm_never_duplicates_a_byte` | pass |
| 22 | No duplicate project / media | `test_22_replay_storm_plus_finalize_replays_create_one_project_and_media_set` | pass |
| 23 | No duplicate processing jobs | `test_23_no_duplicate_processing_jobs_even_when_enqueue_first_fails` | pass |
| + | Adaptive sizing / hysteresis / clamps | `test_adaptive_chunk_size_tracks_measured_throughput_without_oscillating` | pass |
| + | Never exceeds server ceiling | `test_chunk_size_never_exceeds_the_server_declared_ceiling` | pass |
| + | Resync costs no round-trip | `test_offset_mismatch_resync_costs_no_extra_round_trip` | pass |

**How the client-side scenarios are tested is worth stating explicitly.** 11
through 17 concern *client* policy. Asserting on template strings would prove
the code is present, not that it decides correctly — and retry
classification, backoff bounds, `Retry-After` clamping and fingerprint
rejection are exactly the decisions worth proving. So the uploader's policy
block is DOM-free by construction, marked by two comments, and the test
slices it out of the template and **executes the real shipped JavaScript** in
Node with assertions against its actual return values. Node is guarded with a
skip if unavailable; it is present here (v24.14.1) and all of those tests ran.

Server-side scenarios read the **assembled temp file off disk** and compare
bytes, not just `current_offset` — asserting on the offset alone would not
catch a duplicated or spliced byte.

**Impacted existing suites re-run** (the complete 1900+ suite deliberately
not rerun, per the test strategy):

| Suite | Result |
|---|---|
| `tests/integration/test_resumable_upload.py` | 46 passed |
| `tests/integration/test_upload_edge_hardening.py` | 2 passed |
| `tests/gate_jr/test_marker_selection_upload.py` | 62 passed, 1 skipped (pre-existing Playwright skip) |
| `tests/gate_jr/test_v11_experience_ux.py` | 35 passed |
| `tests/gate_jr/test_v11_commercial_ownership_ux.py` | passed |
| `tests/integration/test_quota_characterization.py` | passed |
| `tests/gate_jr/test_wave7_rate_limit_backoff.py` | passed |

Five assertions in the gate_jr suites initially failed. All five were
string-shape assertions pinned to the old implementation's exact source text
(the fixed chunk-size constant, the average-rate variable name, the inline
error pattern-matching, the `&&` chain in the resume matcher) — plus one that
was genuinely my fault: two of my own code comments used a bandwidth unit
while explaining why a round-trip matters, and the copy guard that forbids
quoting a bandwidth number quite correctly scans the whole page. **The comments
were reworded and that guard was left exactly as strict as it was.** The four
shape guards were re-pointed at the new shapes and gained the two facts that
matter most: a spent retry budget pauses, and file identity is proved by
fingerprint. No guard was weakened or deleted.

## 24. Throttled-network measurements

Real measurements, not estimates. Harness:
`scripts/dev/low_bandwidth_upload_certification.mjs` drives the **real
installed Chrome 151.0.7922.138** over CDP with the uplink genuinely
throttled by `Network.emulateNetworkConditions`, against a real running
ScanStory instance
(`scripts/dev/low_bandwidth_upload_server.py`, isolated temp SQLite + temp
data dirs, one seeded creator). No Playwright, no npm dependency — Node's
global `WebSocket` is enough. The in-page script does not reimplement client
policy: it slices the uploader's policy block out of the template and injects
it, so the chunk sizing and retry classification under test are the ones that
ship. Raw results: `evidence/low_bandwidth/throttled_upload_certification.json`.

Payload 512 KiB, server chunk ceiling 256 KiB (so several chunks flow per
run). CSRF enabled and the token read from the real page.

| Profile | Result | Transfer | Effective | Requests | Retries | Bytes retransmitted | Final chunk |
|---|---|---|---|---|---|---|---|
| 5 Mbps / 100 ms | completed | 1 128 ms | 3.718 Mbps | 2 | 0 | 0 | 256 KiB |
| 2 Mbps / 100 ms | completed | 2 378 ms | 1.764 Mbps | 2 | 0 | 0 | 256 KiB |
| 1 Mbps / 300 ms | completed | 4 849 ms | 0.865 Mbps | 2 | 0 | 0 | 256 KiB |
| 0.6 Mbps / 300 ms | completed | 7 654 ms | 0.548 Mbps | 2 | 0 | 0 | 256 KiB |
| 0.3 Mbps / 700 ms | completed | 15 472 ms | 0.271 Mbps | 2 | 0 | 0 | 256 KiB |
| 0.15 Mbps / 700 ms | completed | 30 195 ms | 0.139 Mbps | 3 | 0 | 0 | 128 KiB |

Final integrity in every run: server `current_offset` == 524 288 == declared
total, session `active` and finalizable, zero duplicate bytes.

Latency profiles exercised: 100 ms, 300 ms, 700 ms. Adaptive sizing is
visible in the last row — the 0.15 Mbps link is the only one where the sizer
dropped below the ceiling, to the 128 KiB floor, and its quality label reads
`very slow` while 0.6/0.3 read `slow` and the faster three read `normal`.

**Three harness pitfalls, recorded in the code so the next person does not
rediscover them:** a fixed CDP debug port silently attaches to a leftover
browser from a previous run (`child.kill()` on Windows kills the launcher,
not the tree); a throw inside the CDP message listener swallows the reply and
looks exactly like a product hang; and Chrome closes the DevTools socket out
from under a `Runtime.evaluate` awaited for more than a few seconds, so a long
run must be kicked off and then polled. The second and third cost real
debugging time and produced two apparent "hangs" that were entirely
harness-side.

## 25. 0.6 Mbps result

**Reliable.** 512 KiB in 7 654 ms, 0.548 Mbps effective (91% of the 0.6 Mbps
cap, the shortfall being the 300 ms latency on each of two requests), 2
requests, 0 retries, 0 bytes retransmitted, chunk held at the 256 KiB
ceiling, final offset exact.

This profile was also the base for all three interruption scenarios (§28),
all of which recovered without losing a byte.

## 26. 0.3 Mbps result

**Slow but reliable and resumable.** 512 KiB in 15 472 ms, 0.271 Mbps
effective, 2 requests, 0 retries, 0 bytes retransmitted, final offset exact.
Quality label `slow`, so the creator sees *"Slow connection detected… the
upload is still going"* rather than anything alarming.

The adaptive sizer held 256 KiB here (~7.5 s per chunk, close to the 8 s
target), so a mid-chunk drop at this speed risks at most ~7.5 s of
re-transfer — and in practice cost 0 bytes, because a failed transport never
had bytes accepted and the replay is accepted rather than duplicated.

## 27. Lowest successfully tested speed

**0.15 Mbps (150 kbps) with 700 ms latency — completed cleanly.** 512 KiB in
30 195 ms, 0.139 Mbps effective, 3 requests, 0 retries, 0 bytes
retransmitted, final offset exact. The sizer correctly dropped to the 128 KiB
floor and labelled the link `very slow`.

Stated precisely, and no further: **0.15 Mbps is the lowest speed measured to
complete an upload in this environment.** No minimum supported speed is being
declared. Nothing below 0.15 Mbps was tested, and the measurement was taken
with a 512 KiB payload over loopback with emulated latency — not a real
cellular radio with real packet loss, jitter, or a carrier-grade NAT timing
out an idle socket. A field minimum needs field data.

## 28. Interruption / recovery results

All three interruption scenarios recovered, and **not one of them
retransmitted a single byte.**

| Scenario | Result | Wall | Requests | Retries | Bytes retransmitted |
|---|---|---|---|---|---|
| 10 s disconnect mid-upload (0.6 Mbps base) | recovered, completed | 26 131 ms | 7 | 4 | 0 |
| 30 s disconnect mid-upload (0.6 Mbps base) | recovered, completed | 40 184 ms | 8 | 5 | 0 |
| Refresh mid-upload (0.6 Mbps base) | resumed from 262 144 B, completed | 3 829 ms transfer | 1 | 0 | 0 |

Network failure mid-chunk is what the disconnect scenarios *are*: the uplink
is cut with a chunk in flight, and the client's classifier routes it to
bounded backoff. The backoff table turns out to be well matched to the
outages tested — 1+2+4+8+15 s covers a 30 s blackout without the retry budget
being spent, which is why the 30 s case recovered rather than pausing.

Refresh mid-upload is the strongest single result in this report: the reload
destroys everything the page knew, and the second half completed anyway
because it asked the server for the truth. The recorded `wallMs` for that row
(908 175) is a harness artifact — the deliberately-abandoned pre-refresh run
polled out its own deadline — and has been fixed in the harness; the real
resume-and-finish transfer time is the 3 829 ms above.

## 29. Proxy / server configuration findings

Reviewed `docs/production/README.md` and `docs/production/deployment-runbook.md`.
Review only; no production config invented.

The existing documentation is already largely correct for this workload and
already favours small resumable requests over giant timeouts. It specifies
`client_max_body_size >= SCANSTORY_RESUMABLE_CHUNK_MAX_BYTES` for `location
/api/uploads/sessions/*/chunk` (and notes it should not greatly exceed it),
`client_max_body_size >= MAX_CONTENT_LENGTH` for the non-resumable upload
locations, a server default `<= MAX_REQUEST_BODY_BYTES` (64 MiB), and
`proxy_read_timeout`/`proxy_send_timeout`/`client_body_timeout` sized to
cover a single chunk comfortably. Q17 and Q18 are already logged as
outstanding server-team questions.

Three findings to add:

1. **The chunk ceiling is now the single source of truth on both sides.** The
   client reads `max_chunk_bytes` from the session payload, so lowering
   `SCANSTORY_RESUMABLE_CHUNK_MAX_BYTES` automatically constrains the client
   instead of causing opaque 413s. The proxy value now only needs to track the
   app config — one number, one direction.
2. **Raising the server ceiling above 5 MiB has no effect.** The client's hard
   maximum is 5 MiB (`RESUMABLE_CHUNK_MAX_BYTES`), by design: bigger chunks
   lose more on a drop. Worth noting so nobody tunes a number that does
   nothing.
3. **`SCANSTORY_UPLOAD_SESSION_ABANDONED_STALE_MINUTES`, not the TTL, is what
   bounds a paused upload** (§17). With the default 120 minutes, an overnight
   pause is reclaimed. If the product wants to support one, this is the value
   to raise — and `cleanup-upload-sessions` needs to be actually scheduled
   (it is a manually invoked CLI, dry-run by default), which is a deployment
   decision rather than a code one.

No infrastructure was invented and no production config file was modified.

## 30. Migration status

**No migration. None was needed.** Every backend change is behavioural or
additive-in-response-body:

- `_upload_api_error(**extra)` — response shape only.
- `max_chunk_bytes` in the session payload — computed from existing config.
- Sliding `expires_at` — a different *value* written to an existing column,
  not a schema change.

No column added, dropped or altered; no index, no constraint, no enum value.
`UploadSession.status` gained nothing, because the two states the mission
names that the server lacks (`PAUSED_NETWORK`, `RESUMING`) are correctly
client-side states over an untouched `active` session. Nothing here needed a
`STOP and justify first`, because nothing here needed a migration.

## 31. Scanner files hash before / after

Untouched, and neither file appears in any commit in this pass.

| File | Before (`b2ac7f3` blob) | After (working tree, LF-normalised) |
|---|---|---|
| `scanner_runtime.py` | `eda140bf24f534e160d365c863c618469d68bbcf9619273d499674590324cec0` | `eda140bf24f534e160d365c863c618469d68bbcf9619273d499674590324cec0` |
| `static/js/scanner-runtime.js` | `05badbd03e00c22715edbdba168db8721ae621493acab8a211a54dbf76acc5b2` | `05badbd03e00c22715edbdba168db8721ae621493acab8a211a54dbf76acc5b2` |

Identical. One note for reproducibility: hashing the working-tree files
*raw* gives
`a092b3f141f4e1ca743e45693db5b3560843b86baf59b853570607174982af16` and
`95d5305dd3f8c1c0d1db84ca90b51fe79b8bb322bf1b1a2a3e771c270b3eb7b3`, because
this repo checks out CRLF while git stores LF. The LF-normalised digests above
match the committed blobs exactly, and `git status` reports neither file as
modified. No ORB, RANSAC, homography, optical-flow, tracking-threshold,
camera-calibration or overlay-geometry code was read for modification, let
alone changed.

## 32. `git diff --check`

Clean — no output, no whitespace errors, no conflict markers.

## 33. `git status --short`

Empty — no output.

Everything is committed, including this report. No stray files and no
leftover scratch output inside the worktree: temporary server logs and
harness stdout were written to the session scratchpad directory, outside the
repository, and the only harness artifact kept in-tree is the measured
`evidence/low_bandwidth/throttled_upload_certification.json`.

## 34. Remaining limitations

Honest list, including the things this pass chose not to do.

1. **The certification exercises the protocol and client policy, not the
   wizard DOM.** It logs into the real page, reads the real CSRF token and
   runs the real policy code against the real endpoints under real
   throttling — but it does not click through the marker-cropping wizard. That
   UI flow remains covered only by the (unchanged, passing) gate_jr tests.
2. **512 KiB payload, and 2 MiB destabilised the harness.** Runs at 2 MiB
   reliably killed the CDP socket in this environment. This is a harness/Chrome
   interaction, not a product limit — the pytest scenarios exercise the same
   protocol at arbitrary sizes — but it does mean the throttled numbers come
   from a small payload with few chunks, so *sustained* adaptive growth over
   dozens of chunks is proven deterministically in pytest rather than in the
   browser.
3. **No sub-0.15 Mbps data, and no real-radio data.** See §27. Loopback with
   emulated latency is not a cellular link.
4. **Multi-pair projects still use the legacy non-resumable uploader.** Every
   improvement here applies to the single-pair path only. A multi-pair upload
   on a weak link is still all-or-nothing. This is the largest remaining gap
   and predates this pass.
5. **A paused upload's real lifetime is the cleanup staleness window** (§17),
   default 2 hours, and cleanup must be scheduled to run at all. "Resume when
   you're ready" is honest for a coffee break, not for a week.
6. **Per-chunk integrity deferred** (§16), with reasoning recorded.
7. **Closing the tab still stops the upload.** The copy is careful never to
   promise otherwise. Fixing it needs a service worker, explicitly out of
   scope.
8. **Auto-resume needs the browser's `online` event.** A link that degrades to
   uselessness without going formally offline pauses and waits for a human.
9. **A session stuck in `finalizing` is still not swept** by the cleanup CLI,
   which only touches `active` rows. Pre-existing and already documented.
10. **The 3 client-policy Node tests skip if Node is absent.** They ran here;
    a CI image without Node would silently lose that coverage.

## 35. Next recommended phase

In priority order.

1. **Bring multi-pair projects onto the resumable path.** This is the biggest
   remaining hole and the one most likely to be hit by a real creator: today a
   two-pair project on a weak link is all-or-nothing. The session model's
   documented one-session-one-pair scope is the thing to revisit.
2. **Decide the paused-upload lifetime as a product question, then configure
   it.** Pick the window ("resume within N hours"), set
   `SCANSTORY_UPLOAD_SESSION_ABANDONED_STALE_MINUTES` to match, schedule
   `cleanup-upload-sessions`, and make the copy say the actual number.
3. **Field measurement on a real radio.** A real device on a real weak
   cellular link, with the telemetry from §22 collected, is what turns "0.15
   Mbps completed in a lab" into a supportable minimum.
4. **Extend the certification harness through the wizard DOM**, and find out
   why 2 MiB destabilises the CDP socket — a fresh target per run is the first
   thing to try.
5. **Sweep `finalizing` sessions** in the cleanup CLI, with a conservative age
   threshold.
6. **Only then** consider per-chunk integrity, and only if a real corruption
   incident justifies the bytes it costs on exactly the links that can least
   afford them.
