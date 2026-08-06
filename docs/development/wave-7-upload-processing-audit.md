# ScanStory V1 Wave 7 Upload + Processing Audit

Branch: `v1/wave-7-upload-processing`  
Base: `5c990795933172aed4eb3002352cf7aca4ec225d`

This is the Phase 1 audit-only deliverable. It maps the existing creator upload and processing pipeline before optimization. No scanner recognition, overlay, payment, model, or migration changes are proposed here.

## 1. Current Pipeline Diagram

```text
Browser file selection
  -> video metadata read
  -> crop/full-image marker selection
  -> marker canvas render/compress
  -> if one pair:
       POST /api/uploads/sessions
       -> sequential chunks to /api/uploads/sessions/<id>/chunk
       -> POST /api/uploads/sessions/<id>/finalize
       -> server validates, creates Project + ProjectPair, generates QR
       -> enqueue_project_pair_processing()
       -> browser optionally polls /api/processing/jobs/<job_id>
       -> redirect to /success/<project_id>
     else:
       legacy multipart POST /upload
       -> server validates all pairs, creates Project + ProjectPairs, generates QR
       -> enqueue_project_pair_processing()
       -> redirect

Worker path:
  processing_operations.run_processing_job(job_id)
    -> mark job processing
    -> load Project and ProjectPairs
    -> for each pair:
         standardize uploaded image, except admin projects
         make feature working JPEG
         extract feature NPZ
         commit pair state
    -> clear load_features cache
    -> mark job completed or failed/retryable
```

## 2. Exact Code Paths

- Contract: `docs/development/resumable-upload-api-contract.md`
- Upload frontend: `templates/user/user_create_project.html`
  - progress DOM: `setUploadProgress()`
  - client timing logs: `uploadClientLog()`, `logClientStage()`
  - resumable constants: `RESUMABLE_UPLOAD_CHUNK_SIZE`, `RESUMABLE_UPLOAD_STORAGE_KEY`, `RESUMABLE_POLL_MAX_ATTEMPTS`
  - session create/status/chunk/finalize/cancel: `createResumableSession()`, `getUploadSessionStatus()`, `uploadResumableChunk()`, `finalizeResumableSession()`, `cancelResumableSession()`
  - sequential stream: `uploadResumableStream()`
  - finalize retry: `finalizeResumableWithBoundedRetry()`
  - single-pair submit: `submitResumableSinglePair()`
  - legacy multi-pair submit: `projectForm` submit handler / XHR branch
- Resumable backend: `app.py`
  - `_upload_identity()`, `_upload_session_owned()`
  - `_upload_session_temp_path()`, `_safe_delete_upload_temp()`
  - `_lock_upload_session()`
  - `_finalize_enqueue_and_complete()`
  - `create_upload_session()`
  - `upload_session_chunk()`
  - `upload_session_status()`
  - `_finalize_assemble_and_validate()`
  - `finalize_upload_session()`
  - `cancel_upload_session()`
  - `cleanup_upload_sessions()`
- Processing queue: `processing_queue.py`
  - `create_processing_job()`
  - `_enqueue_transport()`
  - `enqueue_project_pair_processing()`
  - `mark_job_processing()`, `mark_job_completed()`, `mark_job_failed()`, `retry_failed_job()`
  - `processing_job_status_payload()`
- Worker: `processing_operations.py`
  - `run_processing_job()`
  - `_process_pair()`
- Status endpoint: `app.py` `processing_job_status()`
- Models: `models.py`
  - `UploadSession`
  - `ProcessingJob`
  - `Project`
  - `ProjectPair`
- Migration: `migrations/versions/44340c16353c_resumable_upload_sessions.py`
- Tests:
  - `tests/integration/test_resumable_upload.py`
  - `tests/integration/test_rq_processing_foundation.py`
  - upload UI source tests in `tests/gate_jr/test_marker_selection_upload.py`
  - migration tests in `tests/migrations/test_resumable_upload_migration.py`

## 3. Baseline Timing Evidence

Existing timing is mostly client-side console diagnostics and model timestamps. It is enough for rough sequencing but not enough for production latency budgets.

Current measured/available fields:

- Browser:
  - `upload_id`
  - `pair_count`
  - `video_size_bytes`
  - `bytes_uploaded`
  - `percentage`
  - `elapsed_ms`
  - `estimated_upload_speed`
  - `estimated_remaining_time`
  - `http_status`
  - `current_phase`
  - `preparation_duration_ms`
  - progress DOM visibility snapshots
- Backend rows:
  - `UploadSession.created_at`, `updated_at`, `completed_at`
  - `UploadSession.current_offset`, `expected_total_size`
  - `ProcessingJob.queued_at`, `started_at`, `completed_at`, `failed_at`, `last_heartbeat_at`
  - `ProjectPair.feature_extraction_time`
- Worker:
  - per-pair `start = time.time()` and `feature_extraction_time`

Missing stage timing:

- server chunk request duration
- request body read duration
- disk append/write duration
- finalize total duration
- checksum duration
- image/video validation duration
- quota reservation duration
- Project/ProjectPair DB creation duration
- file move duration
- QR generation duration
- enqueue duration
- queue pickup delay as `started_at - queued_at`
- full time-to-ready as `job.completed_at - upload_session.created_at`
- retry count and categorized network/timeout reasons in durable storage

## 4. Fast-Network Bottlenecks

P1: Fixed 1 MiB chunks may underuse fast Wi-Fi and LAN. `RESUMABLE_UPLOAD_CHUNK_SIZE = 1024 * 1024` means a 1 GiB video requires about 1024 sequential HTTP requests. Each chunk waits for full round-trip, CSRF header setup, Flask route dispatch, full body read, disk append, DB commit, and response before the next chunk.

P1: Chunk route reads each chunk fully into memory with `request.get_data(cache=False)`. Memory is bounded by chunk size, but throughput is coupled to Flask request buffering and Python write/commit per chunk.

P1: Server commits on every chunk. This is correct for resume authority but expensive on high-throughput networks. It can be optimized only carefully, because `current_offset` is the authoritative recovery point.

P1: Finalize is synchronous and can do checksum, validation, quota, Project/Pair DB writes, file moves, QR generation, and enqueue in a single request. A large file plus video validation can make the client wait despite transfer completion.

P1: Browser processing-status polling only runs up to `RESUMABLE_POLL_MAX_ATTEMPTS = 8` with delays starting at 1200 ms. It does not adapt to fast queue completion beyond the first poll and may redirect before worker completion.

P2: Client progress logs are gated behind debug and console-only. There is no durable timing record for aggregate before/after comparison.

P2: Legacy multi-pair upload remains multipart, so Wave 5 resumability benefits apply only to one pair.

## 5. Weak-Network Failure Modes

P0: Finalize response lost after a successful commit is not currently recoverable by the frontend. Backend returns `409 ALREADY_FINALIZED` on second finalize for completed sessions. `finalizeResumableWithBoundedRetry()` does not treat `ALREADY_FINALIZED` as a recoverable signal and does not call status to recover `project_id`. A user can get an upload failure even though exactly one project and one job were already created.

P1: Network failure handling is narrow. `uploadResumableStream()` reconciles on `OFFSET_MISMATCH`, `status === 0`, or `TypeError`. It does not use bounded exponential backoff with jitter, does not classify timeout/offline/background cases, and does not surface "retrying" as a separate user phase beyond "Connection recovered".

P1: Resume state is persisted in localStorage without file bytes, which is correct, but refresh recovery is only attempted after the user selects the same files and submits again. There is no visible "resume previous upload" prompt before file selection.

P1: Cancel is only valid for `active`. If cancel races with finalize, the user may get a generic state conflict and needs clearer guidance.

P1: `finalizing` stuck states are documented as a known V1 gap and are not swept by cleanup. A crash while status is `finalizing` can strand a session.

P1: Processing failure is persisted (`ProjectPair.processing_error`, job safe error fields), but the creator upload page only redirects/polls briefly. There is no creator-facing retry/recovery flow in the upload completion surface.

P1: Client computes upload speed from total offset / elapsed since stream start. After resume from a high offset, speed can be inflated because previous bytes are counted against a new page/session's elapsed time.

P2: Browser backgrounding/mobile screen lock behavior is not explicitly handled. Fetch aborts and timer throttling will fall into generic network/resume paths.

P2: `client_checksum_sha256` is supported but frontend does not send it. That avoids client CPU cost but leaves no end-to-end checksum beyond offset and validation.

## 6. Scalability Risks

P1: Disk and DB pressure scales linearly with chunk count. A fixed 1 MiB chunk size creates many writes and commits for large videos.

P1: SQLite uses whole-database write locking; `_lock_upload_session()` only provides row-level locking on databases that support it. Concurrent creators will serialize more aggressively on SQLite than production databases.

P1: `create_processing_job()` relies on a unique idempotency constraint, but the code first queries `active_project_job()` then inserts. Under true concurrent enqueue, the database constraint is the final guard, but the code path should be tested under competing sessions/workers.

P1: Worker processing is CPU-heavy (`standardize_uploaded_image`, `make_feature_working_jpeg`, `extract_features_multi`) and serial per project job. Queue concurrency must be governed by CPU and memory capacity, not only web request capacity.

P1: Reverse-proxy docs mention security and uploads only as smoke tests. They do not define upload body limits, chunk request timeout, upstream read/send timeout, temp storage location, or Range/media timeout alignment.

P2: `request_limiter` is process-local per production docs. It is not suitable for horizontally scaled upload/session limits.

P2: `cleanup-upload-sessions` sweeps only `active` sessions, by design. It does not batch by file size or emit storage reclaimed metrics.

P2: Processing status endpoint is per-job and authenticated. There is no project-level aggregate status endpoint for the upload-created project after redirect if the job id is lost.

## 7. Security and Idempotency Invariants

Current strong invariants:

- Upload session routes require user/admin session auth.
- Cross-owner session access returns 404.
- CSRF header is required from frontend for upload routes.
- Original filenames are display-only and never used for filesystem paths.
- Temp file path is derived from server UUID storage token only.
- `_safe_delete_upload_temp()` bounds deletion to `TMP_UPLOADS_DIR`.
- `X-Chunk-Offset` must match authoritative `current_offset` or be a fully already-accepted duplicate.
- Duplicate accepted chunks are safe no-op success.
- Finalize uses atomic conditional update from `active` to `finalizing`.
- Project quota is reserved after validation and before Project creation.
- Invalid image/video finalize creates no Project/Pair and consumes no quota.
- Enqueue failure leaves `assembled`, preserving created rows and supporting finalize retry.
- Processing jobs have project idempotency uniqueness.
- Worker replay of completed jobs is safe.

Critical invariants to preserve:

- Exactly one Project per completed UploadSession.
- Exactly one ProjectPair for V1 resumable session.
- Exactly one active processing job for the project.
- Retry must resume from server `current_offset`.
- Failed validation must delete temp data and not consume quota.
- Failed processing must not destroy uploaded media.
- Client must not store file bytes in localStorage.
- No raw filesystem paths, stack traces, secrets, or customer emails in upload telemetry.

## 8. Ranked Findings

### P0

1. **Lost finalize success response can look like failure.**  
   Evidence: backend `finalize_upload_session()` returns `ALREADY_FINALIZED` for completed sessions; frontend finalize retry does not recover by GET status and redirect using populated `project_id`.

2. **No durable timing map for server stages.**  
   Evidence: backend has no structured timing fields around chunk write, finalize validation, QR, enqueue, queue wait, processing duration beyond existing model timestamps and pair feature extraction time.

### P1

1. **Fixed 1 MiB chunks constrain fast-network throughput.**
2. **Weak-network retry lacks exponential backoff with jitter and categories.**
3. **Finalize does too much synchronous work without per-stage timing or timeout evidence.**
4. **Processing completion polling is short and not project-status resilient if job id is absent.**
5. **`finalizing` crash recovery is known but unswept.**
6. **Proxy/upload timeout and body-size alignment is not documented.**
7. **Worker queue pickup delay is not explicitly measured.**
8. **Multi-pair resumable upload is unsupported; legacy path remains all-or-nothing multipart.**

### P2

1. Frontend speed/ETA calculations can be misleading after resume.
2. Optional checksum is unused by frontend.
3. Cleanup CLI lacks reclaimed-bytes reporting.
4. Processing queue monitoring docs still say queue monitoring is future despite RQ code now existing.
5. Creator-facing processing retry/recovery is thin after redirect.

## 9. Proposed Implementation Batches

### Batch A: Timing and observability, low risk

- Add structured timing objects to upload session create/chunk/finalize responses or logs.
- Add backend stage timings:
  - `request_duration_ms`
  - `server_write_duration_ms`
  - `finalize_duration_ms`
  - `checksum_duration_ms`
  - `validation_duration_ms`
  - `project_create_duration_ms`
  - `qr_duration_ms`
  - `enqueue_duration_ms`
- Add worker timings:
  - `queue_wait_duration_ms`
  - `processing_duration_ms`
  - `pair_processing_duration_ms`
- Keep output structured and log-safe; no noisy print statements.

### Batch B: Weak-network correctness

- Treat `ALREADY_FINALIZED` as recoverable in frontend by calling status and redirecting if session is completed with `project_id`.
- Add tests for lost finalize response after successful commit.
- Add bounded exponential backoff with jitter for chunk status reconciliation and queue-enqueue finalize retry.
- Add explicit UI phases: `Retrying upload`, `Reconnecting`, `Finalizing`, `Processing`, `Ready`.

### Batch C: Throughput

- Add adaptive chunk size within sequential contract:
  - conservative start on mobile/unknown network
  - grow after stable fast chunks
  - shrink after timeout/network failure
  - cap memory and request timeout exposure
- Keep one in-flight chunk only.
- Add tests asserting offsets remain sequential and retry uses authoritative offset.

### Batch D: Processing status and recovery

- Extend polling behavior with bounded but longer processing awareness.
- Surface safe failure/retry eligibility from `processing_job_status_payload()`.
- Add creator-visible retry route only if it can reuse `retry_failed_job()` safely.
- Add test coverage for processing failure and retry.

### Batch E: Operations

- Update proxy/runbook docs with chunk-size, max body, upstream timeout, temp storage, worker concurrency, queue monitoring, and cleanup cadence.
- Add cleanup CLI reporting for bytes reclaimed.

## 10. Tests and Measurable Acceptance Targets

Existing tests to preserve/run:

- `tests/integration/test_resumable_upload.py`
- `tests/integration/test_rq_processing_foundation.py`
- `tests/gate_jr/test_marker_selection_upload.py`
- `tests/security`
- `tests/migrations/test_resumable_upload_migration.py`
- quota tests touching project creation
- upload validation tests

New tests:

- resume after interruption uses GET status offset
- duplicate accepted chunk retry remains success
- duplicate finalize does not duplicate Project/Pair/job
- lost finalize response after successful commit recovers via status
- `QUEUE_ENQUEUE_FAILED` retry keeps Project/Pair and enqueues once
- weak-network backoff retries same logical event without busy loop
- refresh/resume status from localStorage metadata without file bytes
- processing failure persists safe error and retry eligibility
- multiple-pair legacy partial failure does not destroy prior working media
- structured timing metadata exists and excludes secrets/paths
- concurrent upload sessions do not cross-own or duplicate project/job

Acceptance targets:

- Exactly one project and one processing job after any successful finalized session.
- Retried accepted chunks do not change file bytes or offset.
- Resume offset always comes from server status after mismatch/network uncertainty.
- Finalize lost-response recovery redirects to the existing completed project.
- Upload UI always shows one of: preparing, uploading, retrying, finalizing, processing, failed, ready.
- Queue wait and processing duration are measurable from persisted timestamps.
- Fast-network 50 MB upload should not be dominated by thousands of tiny RTTs after adaptive chunking.
- Slow/unstable network should resume without restarting from byte zero unless session is terminal.

## 11. Files Likely To Change

Likely implementation files:

- `templates/user/user_create_project.html`
- `app.py`
- `processing_queue.py`
- `processing_operations.py`
- `docs/production/deployment-runbook.md`
- `docs/production/monitoring-alerting.md`
- `docs/production/security-proxy-checklist.md`
- `tests/integration/test_resumable_upload.py`
- `tests/integration/test_rq_processing_foundation.py`
- `tests/gate_jr/test_marker_selection_upload.py`

Possible but avoid unless clearly needed:

- `upload_validation.py`
- `tests/security/*`
- migration tests if no schema change is made

## 12. Areas That Must Not Be Changed

- Scanner recognition, ORB, homography, optical flow, overlay, tracking thresholds, detection cadence.
- Payment, Razorpay, entitlement, quota semantics beyond preserving existing upload quota points.
- Models and migrations during the audit phase.
- RQ worker internals beyond timing/status reporting unless a later approved implementation batch requires it.
- `SCANSTORY_V1_GAP_AUDIT.md`.
- Untracked `instance/`.
- Existing media files or production records.
