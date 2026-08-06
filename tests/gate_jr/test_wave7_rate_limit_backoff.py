"""Wave 7 — 429-vs-cadence fix tests.

See docs/development/wave-7-detection-overlay-audit.md §7/§12 for the full evidence and
rationale. Source-level assertions match the existing style in test_scanner_lifecycle.py (this
repo has no headless browser in CI); the backend/log-shape checks run real code through
app.test_client(). No RATE_LIMITS value, no homography/inlier threshold, is touched or asserted
to have changed here — only client classification/backoff and additive structured logging.
"""
import re
from io import BytesIO
from pathlib import Path

import pytest

pytestmark = pytest.mark.scanner_robustness


def _scanner_html():
    return Path("templates/user/scanner.html").read_text(encoding="utf-8", errors="ignore")


def _scanner_runtime_js():
    return Path("static/js/scanner-runtime.js").read_text(encoding="utf-8", errors="ignore")


def _jpeg_bytes():
    import numpy as np
    import cv2

    img = np.full((600, 800, 3), 40, dtype=np.uint8)
    rng = np.random.default_rng(0)
    img = cv2.add(img, rng.integers(0, 40, size=img.shape, dtype=np.uint8))
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


# ---------------------------------------------------------------------------
# scanner-runtime.js — shared classification/backoff primitives
# ---------------------------------------------------------------------------

def test_runtime_exports_rate_limit_classification_helpers():
    js = _scanner_runtime_js()
    assert "function isRateLimitedResponse(status, payload)" in js
    assert 'if (status === 429) return true;' in js
    assert "function resolveRetryAfterMs(payload, headerValue)" in js
    assert "isRateLimitedResponse, resolveRetryAfterMs" in js


def test_request_policy_gates_on_backoff_deadline():
    js = _scanner_runtime_js()
    start = js.index("function createRequestPolicy(mode)")
    end = js.index("function validateDetectionResponse", start)
    body = js[start:end]
    assert "let backoffUntil = -Infinity;" in body
    assert "if (now < backoffUntil) return false;" in body
    assert "noteRateLimited(now, retryAfterMs)" in body
    assert "resetBackoff()" in body
    # A later, smaller retry-after must never shorten an already-set deadline.
    assert "backoffUntil = Math.max(backoffUntil, now + bounded);" in body


# ---------------------------------------------------------------------------
# scanner.html — the 429 branch must run BEFORE any detection-outcome accounting
# ---------------------------------------------------------------------------

def _detect_once_body():
    html = _scanner_html()
    start = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    end = html.index("async function scanTick(token)", start)
    return html[start:end]


def test_rate_limited_branch_exists_before_detection_accounting():
    body = _detect_once_body()
    json_at = body.index("const data = await r.json();")
    rate_limited_at = body.index("runtime.isRateLimitedResponse(r.status, data)")
    fail_count_at = body.index("detectionFailCount++;")
    validate_at = body.index("runtime.validateDetectionResponse(data)")
    # The 429 check must run immediately after parsing the JSON body, strictly before both
    # the shared validation helper and the no-match failure-count increment.
    assert json_at < rate_limited_at < validate_at
    assert rate_limited_at < fail_count_at


def test_rate_limited_branch_never_falls_through_to_detection_handling():
    body = _detect_once_body()
    rate_limited_at = body.index("if (runtime.isRateLimitedResponse(r.status, data)) {")
    # The branch must return before the session-ending/staleness/validation logic below it.
    next_return = body.index("return;", rate_limited_at)
    session_ending_check_at = body.index("if (sessionEnding) {", rate_limited_at)
    assert rate_limited_at < next_return < session_ending_check_at


def test_rate_limited_branch_notes_backoff_and_releases_in_flight():
    body = _detect_once_body()
    branch_start = body.index("if (runtime.isRateLimitedResponse(r.status, data)) {")
    branch_end = body.index("\n        }", body.index("return;", branch_start))
    branch = body[branch_start:branch_end]
    assert "runtime.resolveRetryAfterMs(data, r.headers.get('Retry-After'))" in branch
    assert "detectionPolicy.noteRateLimited(performance.now(), retryAfterMs);" in branch
    assert "detectionPolicy.finish(requestId);" in branch
    assert "detectionFailCount" not in branch  # must never touch the failure streak
    assert "showRecognitionHelp" not in branch  # must never directly surface a false failure


def test_rate_limited_reschedule_respects_server_advertised_wait():
    body = _detect_once_body()
    branch_start = body.index("if (runtime.isRateLimitedResponse(r.status, data)) {")
    branch = body[branch_start:branch_start + 800]
    assert "Math.max(DETECT_INTERVAL_MS, retryAfterMs)" in branch


# ---------------------------------------------------------------------------
# Continue Scanning / Retry Camera must both reset the backoff cleanly
# ---------------------------------------------------------------------------

def test_continue_scanning_resets_backoff():
    html = _scanner_html()
    start = html.index("function continueScanningFromRecognitionHelp()")
    end = html.index("recognitionContinueBtn.addEventListener", start)
    body = html[start:end]
    assert "detectionFailCount = 0;" in body
    assert "detectionPolicy.resetBackoff();" in body


def test_retry_camera_resets_backoff():
    html = _scanner_html()
    start = html.index("async function retryCameraFromFallback()")
    end = html.index("fallbackRetryBtn.addEventListener", start)
    body = html[start:end]
    assert "detectionFailCount = 0;" in body
    assert "detectionPolicy.resetBackoff();" in body
    assert "scannerGeneration++;" in body


# ---------------------------------------------------------------------------
# Backend contract: the 429 shape the client depends on is exactly what's sent
# ---------------------------------------------------------------------------

def test_detect_init_429_body_and_header_shape(client, project_with_pair):
    project, _pair = project_with_pair
    frame = _jpeg_bytes()
    last_status = None
    last_json = None
    last_headers = None
    for i in range(50):
        resp = client.post(
            "/detect_init",
            data={
                "project_id": str(project.id),
                "scan_session_id": "wave7-429-contract",
                "test_image": (BytesIO(frame), "frame.jpg"),
            },
            content_type="multipart/form-data",
        )
        last_status = resp.status_code
        last_json = resp.get_json()
        last_headers = resp.headers
        if resp.status_code == 429:
            break
    assert last_status == 429, "expected the scanner_init limiter to trip within 50 calls"
    assert last_json["error"] is True
    assert last_json["code"] == "RATE_LIMITED"
    assert isinstance(last_json["retry_after_seconds"], int) and last_json["retry_after_seconds"] > 0
    assert "detected" not in last_json  # the exact shape that used to be misread as NO_MATCH
    assert last_headers.get("Retry-After") == str(last_json["retry_after_seconds"])


# ---------------------------------------------------------------------------
# Backend: structured per-stage timing (Batch 2)
# ---------------------------------------------------------------------------

def test_detect_init_no_match_logs_structured_stage_timings(client, project_with_pair, caplog):
    project, _pair = project_with_pair
    frame = _jpeg_bytes()
    with caplog.at_level("INFO"):
        resp = client.post(
            "/detect_init",
            data={
                "project_id": str(project.id),
                "scan_session_id": "wave7-stage-timing",
                "test_image": (BytesIO(frame), "frame.jpg"),
            },
            content_type="multipart/form-data",
        )
    assert resp.status_code == 200
    assert resp.get_json()["detected"] is False
    records = [r for r in caplog.records if getattr(r, "scanner_latency", None)]
    assert records, "expected a scanner_latency structured log record"
    payload = records[-1].scanner_latency
    assert payload["event"] == "detect_init"
    assert payload["outcome"] == "no_match"
    for stage in ("stage_read_ms", "stage_prep_ms", "stage_detect_ms", "stage_quick_score_ms", "stage_match_ms"):
        assert stage in payload, f"missing {stage} in structured scanner_latency log"
        assert isinstance(payload[stage], (int, float))
        assert payload[stage] >= 0
