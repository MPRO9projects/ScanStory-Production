# ScanStory V1.1 Final Release Blocker Closure Report

## 1. Starting HEAD

`da25e6483fbf814254b5ff5a524292155ce5741b`

## 2. Synced Integration HEAD

`develop/scanstory-v1.1` in `F:\ScanStory-main\ScanStory-integration`:

`da25e6483fbf814254b5ff5a524292155ce5741b`

## 3. Ending HEAD

Commit containing this report. Exact final hash is recorded in the final response.

## 4. Commits

- `fix(v1.1): close release processing and CSP blockers`

## 5. Files Changed

- `app.py`
- `processing_queue.py`
- `tests/integration/test_rq_processing_foundation.py`
- `tests/security/test_v11_final_security_deployment.py`
- `V1_1_FINAL_RELEASE_BLOCKER_CLOSURE_REPORT.md`

## 6. Exact Reprocess Root Cause

Legacy project-pair processing jobs used a permanent per-project idempotency key:

`process_project_pairs:project:<project_id>:pair:-`

The database also enforces `uq_processing_job_project_idempotency` over `(project_id, idempotency_key)`. After initial processing completed, the historical completed job still owned that key. A later explicit creator Fix/Reprocess attempted to create another job with the same key and could hit `IntegrityError` / `UniqueViolation` instead of scheduling a legitimate new processing attempt.

## 7. Reprocess Implementation

- Added `attempt_scope` to the legacy queue scheduling path.
- Default `attempt_scope="initial"` keeps the existing deterministic key for normal initial upload/finalize scheduling.
- Creator `POST /projects/<project_id>/reprocess` now calls `_schedule_project_pair_processing(..., attempt_scope="reprocess")`.
- Explicit reprocess attempts use a new audited key:

  `process_project_pairs:project:<project_id>:pair:-:attempt:<uuid>`

- No processing history is deleted.
- No migration was added.
- Scanner recognition/tracking code was not changed.

## 8. Active-Job Dedupe Behavior

`create_processing_job()` still checks for active project jobs before creating a new row. Active statuses include queued/running/retrying/claimed variants.

If an active job exists, `enqueue_processing_job()` now returns/reuses it immediately, even if `queue_job_id` has not been populated yet. This closes the double-click race where request B could see request A's newly-created active row before request A finished transport enqueue and accidentally enqueue the same job twice.

## 9. Terminal-Job Reprocess Behavior

Terminal historical jobs remain queryable for audit/history. They no longer block a later explicit creator reprocess because the reprocess attempt gets a distinct attempt-specific idempotency key after the active-job check.

## 10. Race / Concurrent-Click Behavior

Concurrent or immediate repeated reprocess requests collapse to the active queued/running attempt. The active row is returned as `created=False`; no second effective work item is transported.

## 11. Creator Route Result

`POST /projects/<project_id>/reprocess`:

- marks project pairs back to processing/extracting;
- schedules with `attempt_scope="reprocess"`;
- returns the existing success redirect/message when scheduling succeeds;
- no longer collides with a completed historical processing job.

## 12. Admin Route Result

No separate admin reprocess route was found in the current backend. Admin project creation uses the shared `_schedule_project_pair_processing()` initial scheduling path, which remains compatible and keeps the original deterministic initial idempotency behavior.

## 13. Postgres Validation

Not completed in this shell.

Evidence:

- Docker API was unavailable: `failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`.
- Local environment variables `DATABASE_URL`, `REDIS_URL`, `SCANSTORY_QUEUE_MODE`, and `RQ_QUEUE_NAME` were absent.
- No connection string or secret value was printed.

The requested `scanstory_v11_p0_cert` database and live worker proof still require an environment where Postgres, Redis, and the RQ worker are available.

## 14. Redis/RQ Validation

Not completed for the same environment reason as Postgres validation.

Automated fake-mode and queue-behavior tests passed. Real Redis/RQ runtime certification remains pending.

## 15. Exact CSP/OpenCV Root Cause

Production CSP was enforced with `script-src` allowing `'wasm-unsafe-eval'` but not `'unsafe-eval'`. The bundled self-hosted `static/js/opencv.js` is an Emscripten-generated runtime that uses dynamic code creation during initialization. Browser verification also showed the bundle attempts a `data:` WASM fetch path that was blocked by scanner-page `connect-src`.

## 16. CSP Solution Considered

Considered:

- replacing/rebuilding OpenCV with a strict-CSP-compatible bundle;
- worker/nonce/hash isolation;
- app-wide CSP relaxation;
- route-scoped scanner CSP relaxation.

Strict-compatible OpenCV rebuild/isolation was too broad for this release-blocker patch and risks scanner runtime churn. App-wide relaxation was rejected.

## 17. CSP Solution Selected

Added a scanner-page-only CSP policy:

- normal app pages keep the existing stricter policy;
- `request.endpoint == "scanner"` receives scanner-specific CSP;
- scanner-specific `script-src` adds `'unsafe-eval'`;
- scanner-specific `connect-src` adds `data:`.

## 18. Security Rationale

The relaxation is scoped to the scanner route only, where the current OpenCV runtime is required for `tracked_overlay` and `detect_once`. Other app pages do not receive `'unsafe-eval'` or `connect-src data:`.

Tests assert both the scanner allowance and the normal-page absence of the allowance.

## 19. Chrome Result

Playwright Chrome against enforced CSP local server:

- `/scanner/1` tracked overlay: HTTP 200, `window.cv.Mat` present, OpenCV initialized, 0 CSP console errors, 0 page errors.
- `/scanner/2` detect once: HTTP 200, `window.cv.Mat` present, OpenCV initialized, 0 CSP console errors, 0 page errors.
- `/scanner/3` direct QR: HTTP 200, OpenCV not loaded, 0 CSP console errors, 0 page errors.

Physical camera certification was not performed; the browser run used fake camera flags.

## 20. Edge Result

Playwright Edge against enforced CSP local server:

- `/scanner/1` tracked overlay: HTTP 200, `window.cv.Mat` present, OpenCV initialized, 0 CSP console errors, 0 page errors.
- `/scanner/2` detect once: HTTP 200, `window.cv.Mat` present, OpenCV initialized, 0 CSP console errors, 0 page errors.
- `/scanner/3` direct QR: HTTP 200, OpenCV not loaded, 0 CSP console errors, 0 page errors.

Physical camera certification was not performed; the browser run used fake camera flags.

## 21. Scanner Mode Results

- `tracked_overlay`: OpenCV requested and initialized.
- `detect_once`: OpenCV requested and initialized.
- `direct_qr`: OpenCV not requested; behavior unchanged.

## 22. Scanner Hashes Before/After

No scanner runtime files were changed.

- `scanner_runtime.py`: `5353A58EF255DDC610609E8551B725479D51FA1F`
- `static/js/scanner-runtime.js`: `519C2F58F30A3B8894991CACB4D86FAE6C907BD3`

## 23. Focused Tests

Passed:

- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m py_compile app.py processing_queue.py`
- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\integration\test_rq_processing_foundation.py -q`
  - `29 passed`
- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\security\test_v11_final_security_deployment.py tests\security\test_csrf_and_headers.py -q`
  - `28 passed`
- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\security\test_v11_final_security_deployment.py tests\security\test_csrf_and_headers.py tests\security\test_runtime_hardening_p0.py tests\security\test_security_health_performance.py -q`
  - `75 passed`
- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\contracts\test_scanner_contract.py tests\gate_jr\test_scanner_lifecycle.py tests\gate_jr\test_gate_jr_scanner_recovery.py -q`
  - `568 passed`
- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\integration\test_rq_processing_foundation.py tests\integration\test_resumable_upload.py -q`
  - initial run found 2 monkeypatch-compatibility failures after adding `attempt_scope`;
  - fixed by preserving the old one-argument call shape for initial scheduling;
  - rerun: `69 passed`.

## 24. Full Regression Result

Passed:

`F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest -q`

Result:

`1952 passed, 4 skipped, 6605 warnings in 4850.36s (1:20:50)`

Skips were:

- browser-level crop test requiring Playwright fixture;
- three PostgreSQL-only migrated-schema lane tests requiring `SCANSTORY_QA_DATABASE_URL`.

## 25. Migration Status

No migration added.

## 26. Remaining Blocker Count

Code blockers addressed: `0` remaining.

Runtime certification blocker remaining: `1` pending environment-dependent validation for Postgres/Redis/RQ using `scanstory_v11_p0_cert`.

## 27. Remaining HIGH Count

`0` code HIGH issues found in this closure pass.

## 28. git diff --check

Passed. Only line-ending warnings were reported by Git for working-copy normalization; no whitespace errors.

## 29. git status --short

Before report:

```text
 M app.py
 M processing_queue.py
 M tests/integration/test_rq_processing_foundation.py
 M tests/security/test_v11_final_security_deployment.py
```

Report file added after that status check.

## 30. Final Recommendation

Conditional merge recommendation.

The two code blockers are closed and full automated regression passes. Complete the requested Postgres/Redis/RQ runtime certification in an environment with the `scanstory_v11_p0_cert` database, Redis, and the `scanstory-processing` RQ worker before final release sign-off.
