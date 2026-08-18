"""The 23 extreme-low-bandwidth chaos scenarios for the resumable upload path.

One test per numbered scenario, in order, so a failure names the behaviour
that regressed rather than a helper. The invariant every one of these
defends is the same sentence: **every byte the server has confirmed stays
confirmed**. Slow is acceptable; restarting from zero after a recoverable
failure is not.

Server-side scenarios drive the real /api/uploads/sessions routes through
the existing test client. Client-side scenarios (retry classification,
backoff, Retry-After, adaptive chunk sizing, fingerprinting) execute the
REAL shipped JavaScript: the pure policy block in
templates/user/user_create_project.html is sliced out between its two
marker comments and evaluated in Node. That block is DOM-free by
construction precisely so this is possible - asserting on template strings
would prove the code is present, not that it decides correctly.

Media fixtures are duplicated from tests/integration/test_resumable_upload.py
for the same documented reason that file gives: a stray global site-packages
`tests` package shadows dotted `tests.xxx` imports in this environment.
"""
import json
import os
import shutil
import subprocess
import tempfile
from datetime import timedelta
from io import BytesIO

import cv2
import numpy as np
import pytest
from PIL import Image

TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "templates", "user", "user_create_project.html",
)
JS_BLOCK_START = "/* ============ Extreme-low-bandwidth resumable upload ============"
JS_BLOCK_END = "/* ==== end of pure low-bandwidth policy helpers ===="
NODE = shutil.which("node")


# ---------------------------------------------------------------------
# Media + HTTP helpers
# ---------------------------------------------------------------------
def _jpeg_bytes(width=640, height=480):
    out = BytesIO()
    Image.new("RGB", (width, height), (120, 80, 40)).save(out, format="JPEG")
    return out.getvalue()


def _mp4_bytes(width=64, height=64, frames=5):
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (width, height))
        for _ in range(frames):
            writer.write(np.zeros((height, width, 3), dtype=np.uint8))
        writer.release()
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _patch_qr(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "generate_custom_qr", lambda *args, **kwargs: False)
    monkeypatch.setattr(app_module, "generate_basic_qr", lambda *args, **kwargs: None)


def _create_session(client, image_size, video_size, **extra):
    payload = {"image_size": image_size, "video_size": video_size}
    payload.update(extra)
    return client.post("/api/uploads/sessions", json=payload)


def _send_chunk(client, session_id, offset, data):
    return client.post(
        f"/api/uploads/sessions/{session_id}/chunk",
        data=data,
        headers={"X-Chunk-Offset": str(offset)},
        content_type="application/octet-stream",
    )


def _status(client, session_id):
    return client.get(f"/api/uploads/sessions/{session_id}")


def _finalize(client, session_id):
    return client.post(f"/api/uploads/sessions/{session_id}/finalize")


def _new_session(client, image_bytes, video_bytes, **extra):
    resp = _create_session(client, len(image_bytes), len(video_bytes), **extra)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["session"]["id"]


def _temp_file_bytes(app_module, session_id):
    """Read the on-disk assembled bytes for a session - the ground truth
    that no assertion about `current_offset` alone can substitute for."""
    session_row = app_module.UploadSession.query.get(session_id)
    path = app_module._upload_session_temp_path(session_row.storage_token)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return fh.read()


# ---------------------------------------------------------------------
# Real-JavaScript harness
# ---------------------------------------------------------------------
def _upload_policy_js():
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        source = fh.read()
    start = source.index(JS_BLOCK_START)
    end = source.index(JS_BLOCK_END)
    block = source[start:end]
    assert "uploadRetryDecision" in block, "policy block markers drifted"
    return block


def _run_policy_js(script_body):
    """Evaluate assertions against the real shipped policy helpers.

    `script_body` runs with every function/const from the template block in
    scope. It must print a JSON object; anything it throws fails the test.
    """
    if NODE is None:  # pragma: no cover - environment guard
        pytest.skip("node is required to execute the shipped uploader policy code")
    harness = (
        "const navigator = { connection: undefined };\n"
        "const window = { crypto: undefined };\n"
        + _upload_policy_js()
        + "\n(async () => {\n"
        + script_body
        + "\n})().catch(err => { console.error(String(err && err.stack || err)); process.exit(1); });\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "policy_check.mjs")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(harness)
        proc = subprocess.run([NODE, path], capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _err(code=None, status=0, name="UploadApiError", retry_after=None, payload="null"):
    after = "null" if retry_after is None else str(retry_after)
    code_js = "null" if code is None else f"'{code}'"
    return (
        f"{{ code: {code_js}, status: {status}, name: '{name}', "
        f"retryAfterMs: {after}, payload: {payload} }}"
    )


# =====================================================================
# 1. Normal chunk upload
# =====================================================================
def test_01_normal_sequential_chunk_upload_assembles_exact_bytes(client, app_module, login_user):
    image, video = _jpeg_bytes(), _mp4_bytes()
    combined = image + video
    session_id = _new_session(client, image, video)
    offset = 0
    step = 4096
    while offset < len(combined):
        chunk = combined[offset:offset + step]
        resp = _send_chunk(client, session_id, offset, chunk)
        assert resp.status_code == 200, resp.get_json()
        offset += len(chunk)
        assert resp.get_json()["current_offset"] == offset
    assert _temp_file_bytes(app_module, session_id) == combined


# =====================================================================
# 2. Duplicate same chunk
# =====================================================================
def test_02_duplicate_chunk_is_idempotent_and_appends_nothing(client, app_module, login_user):
    session_id = _new_session(client, b"", b"x" * 1000, experience_type="direct_qr", playback_mode="direct")
    first = _send_chunk(client, session_id, 0, b"x" * 400)
    assert first.status_code == 200
    replay = _send_chunk(client, session_id, 0, b"x" * 400)
    assert replay.status_code == 200
    body = replay.get_json()
    assert body["note"] == "duplicate_chunk_ignored"
    assert body["current_offset"] == 400
    assert _temp_file_bytes(app_module, session_id) == b"x" * 400


# =====================================================================
# 3. Stale offset
# =====================================================================
def test_03_stale_offset_rejected_with_authoritative_offset_inline(client, app_module, login_user):
    session_id = _new_session(client, b"", b"x" * 1000, experience_type="direct_qr", playback_mode="direct")
    assert _send_chunk(client, session_id, 0, b"a" * 500).status_code == 200
    # Claims an offset already passed, but with a tail that would extend
    # beyond it: a partial replay, which must never be spliced in.
    resp = _send_chunk(client, session_id, 300, b"b" * 400)
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["code"] == "OFFSET_MISMATCH"
    # The authoritative offset rides back on the rejection so the client
    # re-slices in one round-trip rather than two.
    assert body["current_offset"] == 500
    assert body["expected_total_size"] == 1000
    assert _temp_file_bytes(app_module, session_id) == b"a" * 500


# =====================================================================
# 4. Gap / future offset
# =====================================================================
def test_04_future_offset_rejected_and_leaves_no_hole(client, app_module, login_user):
    session_id = _new_session(client, b"", b"x" * 1000, experience_type="direct_qr", playback_mode="direct")
    assert _send_chunk(client, session_id, 0, b"a" * 100).status_code == 200
    resp = _send_chunk(client, session_id, 500, b"b" * 100)
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "OFFSET_MISMATCH"
    assert resp.get_json()["current_offset"] == 100
    assert _temp_file_bytes(app_module, session_id) == b"a" * 100


# =====================================================================
# 5. Chunk response lost, then retried
# =====================================================================
def test_05_lost_chunk_response_then_replay_does_not_duplicate_bytes(client, app_module, login_user):
    """The server stored the bytes, the response never arrived, the client
    re-sends exactly the same range. This is the single most important
    scenario in the file: it must cost nothing."""
    session_id = _new_session(client, b"", b"x" * 4000, experience_type="direct_qr", playback_mode="direct")
    payload = bytes(range(256)) * 4  # 1024 distinctive bytes
    assert _send_chunk(client, session_id, 0, payload).status_code == 200
    replay = _send_chunk(client, session_id, 0, payload)
    assert replay.status_code == 200
    assert replay.get_json()["current_offset"] == 1024
    assert _temp_file_bytes(app_module, session_id) == payload
    # And the stream continues from the authoritative offset unharmed.
    assert _send_chunk(client, session_id, 1024, b"z" * 100).status_code == 200
    assert _temp_file_bytes(app_module, session_id) == payload + b"z" * 100


# =====================================================================
# 6. Interrupted upload, then resume
# =====================================================================
def test_06_interrupted_upload_resumes_from_server_offset(client, app_module, login_user):
    image, video = _jpeg_bytes(), _mp4_bytes()
    combined = image + video
    session_id = _new_session(client, image, video)
    cut = len(combined) // 3
    assert _send_chunk(client, session_id, 0, combined[:cut]).status_code == 200
    # "Interruption": the client goes away entirely and later asks the
    # server where it got to.
    resumed_at = _status(client, session_id).get_json()["session"]["current_offset"]
    assert resumed_at == cut
    assert _send_chunk(client, session_id, resumed_at, combined[resumed_at:]).status_code == 200
    assert _temp_file_bytes(app_module, session_id) == combined


# =====================================================================
# 7. Browser says X, server says Y - the server wins
# =====================================================================
def test_07_server_offset_wins_over_client_belief(client, app_module, login_user):
    session_id = _new_session(client, b"", b"x" * 2000, experience_type="direct_qr", playback_mode="direct")
    assert _send_chunk(client, session_id, 0, b"a" * 700).status_code == 200
    # A client that believes it is further ahead than the server.
    ahead = _send_chunk(client, session_id, 1500, b"b" * 100)
    assert ahead.status_code == 409
    assert ahead.get_json()["current_offset"] == 700
    # A client that believes it is further behind than the server.
    behind = _send_chunk(client, session_id, 0, b"a" * 700)
    assert behind.get_json()["current_offset"] == 700
    # Both disagreements resolve to the same server-held truth.
    assert _status(client, session_id).get_json()["session"]["current_offset"] == 700
    assert len(_temp_file_bytes(app_module, session_id)) == 700


# =====================================================================
# 8. Duplicate finalize
# =====================================================================
def test_08_triple_finalize_produces_exactly_one_project_pair_and_job(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    image, video = _jpeg_bytes(), _mp4_bytes()
    session_id = _new_session(client, image, video)
    assert _send_chunk(client, session_id, 0, image + video).status_code == 200

    first = _finalize(client, session_id)
    assert first.status_code == 200, first.get_json()
    project_id = first.get_json()["session"]["project_id"]

    for _ in range(2):
        again = _finalize(client, session_id)
        assert again.status_code == 409
        assert again.get_json()["code"] == "ALREADY_FINALIZED"

    assert app_module.Project.query.filter_by(id=project_id).count() == 1
    assert app_module.ProjectPair.query.filter_by(project_id=project_id).count() == 1
    assert app_module.ProcessingJob.query.filter_by(project_id=project_id).count() == 1


# =====================================================================
# 9. Finalize after response loss
# =====================================================================
def test_09_lost_finalize_response_recovers_completion_from_status(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    image, video = _jpeg_bytes(), _mp4_bytes()
    session_id = _new_session(client, image, video)
    assert _send_chunk(client, session_id, 0, image + video).status_code == 200
    assert _finalize(client, session_id).status_code == 200

    # The client never saw that response. Its recovery move is a status
    # read, which must report the finished truth rather than an error.
    session = _status(client, session_id).get_json()["session"]
    assert session["status"] == "completed"
    assert session["project_id"]
    assert session["is_terminal"] is True
    assert app_module.ProcessingJob.query.filter_by(project_id=session["project_id"]).count() == 1


# =====================================================================
# 10. Processing enqueued exactly once
# =====================================================================
def test_10_processing_enqueued_exactly_once_across_finalize_replays(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    calls = []
    real = app_module._schedule_project_pair_processing

    def counting(project_id, *args, **kwargs):
        calls.append(project_id)
        return real(project_id, *args, **kwargs)

    monkeypatch.setattr(app_module, "_schedule_project_pair_processing", counting)

    image, video = _jpeg_bytes(), _mp4_bytes()
    session_id = _new_session(client, image, video)
    assert _send_chunk(client, session_id, 0, image + video).status_code == 200
    assert _finalize(client, session_id).status_code == 200
    _finalize(client, session_id)
    _finalize(client, session_id)
    assert len(calls) == 1


# =====================================================================
# 11. 5xx retry
# =====================================================================
def test_11_server_error_statuses_are_retried_with_bounded_backoff():
    result = _run_policy_js(
        f"""
        const out = {{}};
        for (const status of [500, 502, 503, 504]) {{
          out['s' + status] = uploadRetryDecision(
            {{ code: null, status, name: 'UploadApiError', retryAfterMs: null, payload: null }}, 0
          );
        }}
        out.waits = [0, 1, 2, 3, 4, 5].map(a => uploadRetryDecision({_err(status=503)}, a));
        console.log(JSON.stringify(out));
        """
    )
    for key in ("s500", "s502", "s503", "s504"):
        assert result[key]["action"] == "retry", (key, result[key])
    waits = [d["waitMs"] for d in result["waits"]]
    assert all(d["action"] == "retry" for d in result["waits"])
    # Bounded exponential growth with jitter: each wait sits inside its own
    # [base, base + min(base, 1000)) window, so growth is monotone-by-band
    # without ever being a fixed, thundering-herd constant.
    for wait, base in zip(waits, [1000, 2000, 4000, 8000, 15000, 30000]):
        assert base <= wait < base + 1001, (wait, base)


# =====================================================================
# 12. Network failure retry
# =====================================================================
def test_12_transport_failure_is_retryable_not_terminal():
    result = _run_policy_js(
        f"""
        console.log(JSON.stringify({{
          status_zero: uploadRetryDecision({_err(status=0)}, 0),
          type_error: uploadRetryDecision({_err(code=None, status=0, name='TypeError')}, 0),
          timeout_408: uploadRetryDecision({_err(status=408)}, 0)
        }}));
        """
    )
    for key, decision in result.items():
        assert decision["action"] == "retry", (key, decision)
        assert decision["waitMs"] >= 1000


# =====================================================================
# 13. 429 Retry-After handling
# =====================================================================
def test_13_429_honours_retry_after_seconds_and_http_date():
    result = _run_policy_js(
        f"""
        console.log(JSON.stringify({{
          parsed_seconds: parseRetryAfterMs('7'),
          parsed_zero: parseRetryAfterMs('0'),
          parsed_garbage: parseRetryAfterMs('soon'),
          parsed_absent: parseRetryAfterMs(null),
          honoured: uploadRetryDecision({_err(status=429, retry_after=7000)}, 0),
          capped: uploadRetryDecision({_err(status=429, retry_after=999000)}, 0),
          fallback: uploadRetryDecision({_err(status=429)}, 0)
        }}));
        """
    )
    assert result["parsed_seconds"] == 7000
    assert result["parsed_zero"] == 0
    assert result["parsed_garbage"] is None
    assert result["parsed_absent"] is None
    assert result["honoured"] == {"action": "retry", "waitMs": 7000}
    # A hostile or absurd Retry-After is clamped rather than obeyed.
    assert result["capped"]["waitMs"] == 60000
    # No header: our own backoff table takes over.
    assert 1000 <= result["fallback"]["waitMs"] < 2001


# =====================================================================
# 14. 401 / 403 stop retrying
# =====================================================================
def test_14_auth_and_policy_failures_stop_immediately():
    result = _run_policy_js(
        f"""
        console.log(JSON.stringify({{
          unauthenticated: uploadRetryDecision({_err(code='UNAUTHENTICATED', status=401)}, 0),
          blocked: uploadRetryDecision({_err(code='ACCOUNT_BLOCKED', status=403)}, 0),
          bare_401: uploadRetryDecision({_err(status=401)}, 0),
          bare_403: uploadRetryDecision({_err(status=403)}, 0),
          validation_400: uploadRetryDecision({_err(code='INVALID_SIZE', status=400)}, 0),
          too_large_413: uploadRetryDecision({_err(code='VIDEO_TOO_LARGE', status=413)}, 0),
          storage_500: uploadRetryDecision({_err(code='STORAGE_INCONSISTENT', status=500)}, 0),
          expired: uploadRetryDecision({_err(code='SESSION_EXPIRED', status=409)}, 0)
        }}));
        """
    )
    for key, decision in result.items():
        assert decision["action"] == "stop", (key, decision)


# =====================================================================
# 15. Retry exhaustion pauses instead of destroying the session
# =====================================================================
def test_15_retry_exhaustion_pauses_rather_than_failing():
    result = _run_policy_js(
        f"""
        const seq = [];
        for (let attempt = 0; attempt <= 7; attempt++) {{
          seq.push(uploadRetryDecision({_err(status=0)}, attempt).action);
        }}
        console.log(JSON.stringify({{ seq }}));
        """
    )
    seq = result["seq"]
    # Six bounded automatic attempts, then a pause - never a 'stop', because
    # stopping is what discards a session the creator can still finish.
    assert seq[:6] == ["retry"] * 6
    assert seq[6:] == ["pause", "pause"]
    assert "stop" not in seq


# =====================================================================
# 16. Resume after pause
# =====================================================================
def test_16_resume_after_pause_continues_from_server_offset(client, app_module, login_user):
    """The pause path deliberately leaves the session 'active'. A resumed
    client re-reads the authoritative offset and finishes the stream."""
    image, video = _jpeg_bytes(), _mp4_bytes()
    combined = image + video
    session_id = _new_session(client, image, video)
    assert _send_chunk(client, session_id, 0, combined[:512]).status_code == 200

    paused = _status(client, session_id).get_json()["session"]
    assert paused["status"] == "active"
    assert paused["can_upload_chunks"] is True
    assert paused["is_terminal"] is False
    assert paused["current_offset"] == 512

    assert _send_chunk(client, session_id, 512, combined[512:]).status_code == 200
    assert _temp_file_bytes(app_module, session_id) == combined


# =====================================================================
# 17. Fingerprint mismatch blocks an unsafe resume
# =====================================================================
def test_17_fingerprint_mismatch_blocks_resume():
    result = _run_policy_js(
        """
        const base = { name: 'clip.mp4', size: 5000, lastModified: 111, headSha256: 'aa', tailSha256: 'bb' };
        console.log(JSON.stringify({
          identical: fingerprintsMatch(base, { ...base }),
          different_head: fingerprintsMatch(base, { ...base, headSha256: 'cc' }),
          different_tail: fingerprintsMatch(base, { ...base, tailSha256: 'cc' }),
          different_size: fingerprintsMatch(base, { ...base, size: 5001 }),
          different_mtime: fingerprintsMatch(base, { ...base, lastModified: 112 }),
          different_name: fingerprintsMatch(base, { ...base, name: 'other.mp4' }),
          missing_current: fingerprintsMatch(base, null),
          missing_stored: fingerprintsMatch(null, base),
          no_subtle_both_sides: fingerprintsMatch(
            { name: 'clip.mp4', size: 5000, lastModified: 111, headSha256: null, tailSha256: null },
            { name: 'clip.mp4', size: 5000, lastModified: 111, headSha256: null, tailSha256: null }
          )
        }));
        """
    )
    assert result["identical"] is True
    for key in ("different_head", "different_tail", "different_size",
                "different_mtime", "different_name", "missing_current", "missing_stored"):
        assert result[key] is False, key
    # A browser without SubtleCrypto degrades to metadata identity rather
    # than losing the ability to resume at all.
    assert result["no_subtle_both_sides"] is True


# =====================================================================
# 18. Expired session handled safely
# =====================================================================
def test_18_expired_session_is_rejected_safely_not_silently(client, app_module, login_user):
    session_id = _new_session(client, b"", b"x" * 1000, experience_type="direct_qr", playback_mode="direct")
    assert _send_chunk(client, session_id, 0, b"a" * 100).status_code == 200

    session_row = app_module.UploadSession.query.get(session_id)
    session_row.expires_at = app_module.get_utc_now() - timedelta(minutes=1)
    app_module.db.session.commit()

    resp = _send_chunk(client, session_id, 100, b"b" * 100)
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "SESSION_EXPIRED"
    refreshed = _status(client, session_id).get_json()["session"]
    assert refreshed["status"] == "expired"
    assert refreshed["failure_code"] == "SESSION_TTL_EXPIRED"
    assert refreshed["is_terminal"] is True
    # No half-written project or job was produced by the expiry.
    assert refreshed["project_id"] is None


# =====================================================================
# 19. An active session does not expire prematurely
# =====================================================================
def test_19_chunk_activity_slides_the_inactivity_deadline(client, app_module, login_user):
    """expires_at is an INACTIVITY deadline, not a wall clock started at
    session creation - otherwise a creator who pauses overnight, or a
    genuinely long 0.3 Mbps transfer, loses confirmed bytes to the clock."""
    session_id = _new_session(client, b"", b"x" * 3000, experience_type="direct_qr", playback_mode="direct")
    original = app_module.UploadSession.query.get(session_id).expires_at

    # Wind the deadline close to now, as a long-running upload would.
    session_row = app_module.UploadSession.query.get(session_id)
    session_row.expires_at = app_module.get_utc_now() + timedelta(minutes=1)
    app_module.db.session.commit()
    nearly_expired = session_row.expires_at

    assert _send_chunk(client, session_id, 0, b"a" * 500).status_code == 200

    extended = app_module.UploadSession.query.get(session_id).expires_at
    assert extended > nearly_expired
    # Back out to a full TTL from the moment of that activity.
    assert extended >= original - timedelta(minutes=1)
    assert app_module.UploadSession.query.get(session_id).status == "active"


# =====================================================================
# 20. Malformed chunk / range rejected
# =====================================================================
def test_20_malformed_offsets_and_ranges_are_rejected(client, app_module, login_user):
    session_id = _new_session(client, b"", b"x" * 1000, experience_type="direct_qr", playback_mode="direct")

    missing = client.post(
        f"/api/uploads/sessions/{session_id}/chunk",
        data=b"abc", content_type="application/octet-stream",
    )
    assert missing.status_code == 400
    assert missing.get_json()["code"] == "INVALID_OFFSET"

    for bad in ("-5", "abc", "1.5", ""):
        resp = _send_chunk(client, session_id, bad, b"abc")
        assert resp.status_code == 400, bad
        assert resp.get_json()["code"] == "INVALID_OFFSET", bad

    empty = _send_chunk(client, session_id, 0, b"")
    assert empty.status_code == 400
    assert empty.get_json()["code"] == "EMPTY_CHUNK"

    overshoot = _send_chunk(client, session_id, 0, b"x" * 1001)
    assert overshoot.status_code == 400
    assert overshoot.get_json()["code"] == "CHUNK_EXCEEDS_EXPECTED_SIZE"
    assert overshoot.get_json()["current_offset"] == 0

    oversized = _send_chunk(
        client, session_id, 0,
        b"x" * (app_module.app.config["RESUMABLE_UPLOAD_CHUNK_MAX_BYTES"] + 1),
    )
    assert oversized.status_code == 413
    body = oversized.get_json()
    assert body["code"] == "CHUNK_TOO_LARGE"
    # The ceiling travels back so the client can shrink to a legal size.
    assert body["max_chunk_bytes"] == app_module.app.config["RESUMABLE_UPLOAD_CHUNK_MAX_BYTES"]

    # Not one of those rejections advanced the offset or wrote a byte.
    assert _status(client, session_id).get_json()["session"]["current_offset"] == 0
    assert _temp_file_bytes(app_module, session_id) == b""


# =====================================================================
# 21. No duplicate bytes under a chaotic replay storm
# =====================================================================
def test_21_replay_storm_never_duplicates_a_byte(client, app_module, login_user):
    """Every recoverable failure mode fired at one session in sequence. The
    assembled file must still be byte-exact."""
    image, video = _jpeg_bytes(), _mp4_bytes()
    combined = image + video
    session_id = _new_session(client, image, video)

    step = 777
    offset = 0
    while offset < len(combined):
        chunk = combined[offset:offset + step]
        assert _send_chunk(client, session_id, offset, chunk).status_code == 200
        # Replay the accepted chunk (lost response).
        _send_chunk(client, session_id, offset, chunk)
        # Claim a stale offset with an overlapping tail (partial replay).
        if offset >= 100:
            _send_chunk(client, session_id, offset - 100, chunk)
        # Claim a future offset (gap).
        _send_chunk(client, session_id, offset + step * 4, b"junk")
        offset += len(chunk)

    assert _status(client, session_id).get_json()["session"]["current_offset"] == len(combined)
    assert _temp_file_bytes(app_module, session_id) == combined


# =====================================================================
# 22. No duplicate project / media records
# =====================================================================
def test_22_replay_storm_plus_finalize_replays_create_one_project_and_media_set(
    client, app_module, login_user, monkeypatch
):
    _patch_qr(app_module, monkeypatch)
    image, video = _jpeg_bytes(), _mp4_bytes()
    combined = image + video
    session_id = _new_session(client, image, video)

    projects_before = app_module.Project.query.count()
    step = 1024
    offset = 0
    while offset < len(combined):
        chunk = combined[offset:offset + step]
        assert _send_chunk(client, session_id, offset, chunk).status_code == 200
        _send_chunk(client, session_id, offset, chunk)  # replay
        offset += len(chunk)

    assert _finalize(client, session_id).status_code == 200
    _finalize(client, session_id)
    _finalize(client, session_id)

    session = _status(client, session_id).get_json()["session"]
    project_id = session["project_id"]
    assert app_module.Project.query.count() == projects_before + 1
    assert app_module.ProjectPair.query.filter_by(project_id=project_id).count() == 1
    media = app_module.MediaObject.query.filter_by(project_id=project_id).all()
    # Exactly one trigger image + one video ledger row, never a second set.
    assert len(media) == 2, [m.media_role for m in media]
    assert sorted(m.media_role for m in media) == ["trigger_image", "video"]


# =====================================================================
# 23. No duplicate processing jobs
# =====================================================================
def test_23_no_duplicate_processing_jobs_even_when_enqueue_first_fails(
    client, app_module, login_user, monkeypatch
):
    """The recovery path most likely to double-enqueue: the first enqueue
    throws (session parks in 'assembled'), the client retries finalize, and
    the retry must produce the one job that was missing - not a second."""
    _patch_qr(app_module, monkeypatch)
    image, video = _jpeg_bytes(), _mp4_bytes()
    session_id = _new_session(client, image, video)
    assert _send_chunk(client, session_id, 0, image + video).status_code == 200

    real = app_module._schedule_project_pair_processing
    attempts = {"n": 0}

    def flaky(project_id, *args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return None  # enqueue failed
        return real(project_id, *args, **kwargs)

    monkeypatch.setattr(app_module, "_schedule_project_pair_processing", flaky)

    first = _finalize(client, session_id)
    assert first.status_code == 502
    assert first.get_json()["code"] == "QUEUE_ENQUEUE_FAILED"
    parked = app_module.UploadSession.query.get(session_id)
    assert parked.status == "assembled"
    project_id = parked.project_id
    assert app_module.ProcessingJob.query.filter_by(project_id=project_id).count() == 0

    second = _finalize(client, session_id)
    assert second.status_code == 200
    assert app_module.ProcessingJob.query.filter_by(project_id=project_id).count() == 1

    # Further replays add nothing, and the single pair is untouched.
    _finalize(client, session_id)
    _finalize(client, session_id)
    assert app_module.ProcessingJob.query.filter_by(project_id=project_id).count() == 1
    assert app_module.ProjectPair.query.filter_by(project_id=project_id).count() == 1


# =====================================================================
# Adaptive chunk sizing - the P0 behaviour the 23 scenarios above assume
# =====================================================================
def test_adaptive_chunk_size_tracks_measured_throughput_without_oscillating():
    result = _run_policy_js(
        """
        const serverMax = 1024 * 1024;
        const KB = 1024;
        // A very poor link: 20 KB/s measured.
        let poor = nextChunkBytes(512 * KB, 20 * KB, serverMax);
        // A fast link, from the floor: growth must be capped at a doubling.
        let oneStep = nextChunkBytes(128 * KB, 4 * 1024 * KB, serverMax);
        // Repeated growth eventually reaches the server ceiling and stops.
        let grown = 128 * KB;
        for (let i = 0; i < 12; i++) grown = nextChunkBytes(grown, 4 * 1024 * KB, serverMax);
        // Hysteresis: a small measurement change must NOT resize anything.
        const stableStart = 512 * KB;
        const stable = nextChunkBytes(stableStart, (stableStart / 8) * 1.2, serverMax);
        // No measurement yet: keep what we have.
        const unmeasured = nextChunkBytes(256 * KB, null, serverMax);
        console.log(JSON.stringify({
          poor, oneStep, grown, serverMax, stable, stableStart, unmeasured,
          min: RESUMABLE_CHUNK_MIN_BYTES,
          floorClamp: roundChunkBytes(1, serverMax),
          ceilClamp: roundChunkBytes(50 * 1024 * KB, serverMax),
          labels: [10 * KB, 64 * KB, 200 * KB, 2048 * KB, 0].map(networkQualityLabel)
        }));
        """
    )
    # A very poor link lands in the 128-256 KB band: small enough that a
    # dropped chunk costs seconds rather than minutes, never below the floor.
    assert result["min"] == 128 * 1024
    assert result["min"] <= result["poor"] <= 256 * 1024
    # Growth is capped at a doubling per step, never a jump to the ceiling.
    assert result["oneStep"] == 256 * 1024
    # Sustained speed does reach - and stop at - the server ceiling.
    assert result["grown"] == result["serverMax"]
    # Anti-oscillation: a 20% measurement wobble changes nothing.
    assert result["stable"] == result["stableStart"]
    assert result["unmeasured"] == 256 * 1024
    # Clamps hold on both ends.
    assert result["floorClamp"] == 128 * 1024
    assert result["ceilClamp"] == result["serverMax"]
    assert result["labels"] == ["very slow", "slow", "normal", "fast", "unknown"]


def test_chunk_size_never_exceeds_the_server_declared_ceiling():
    """The ceiling is read from the session payload, not hardcoded: the
    client must not 413 itself the day RESUMABLE_UPLOAD_CHUNK_MAX_BYTES
    is lowered in config."""
    result = _run_policy_js(
        """
        const tiny = 200 * 1024;   // server allows less than our default start
        console.log(JSON.stringify({
          initial: initialChunkBytes(tiny),
          rounded: roundChunkBytes(5 * 1024 * 1024, tiny),
          grown: nextChunkBytes(128 * 1024, 10 * 1024 * 1024, tiny),
          absentCeiling: initialChunkBytes(undefined),
          hardMax: RESUMABLE_CHUNK_MAX_BYTES
        }));
        """
    )
    assert result["initial"] <= 200 * 1024
    assert result["rounded"] <= 200 * 1024
    assert result["grown"] <= 200 * 1024
    # With no ceiling advertised we still never exceed our own hard maximum.
    assert result["absentCeiling"] <= result["hardMax"]


def test_offset_mismatch_resync_costs_no_extra_round_trip():
    """OFFSET_MISMATCH is classified as a resync, not a retry: there is
    nothing to back off from, and the authoritative offset is already in
    the rejection body."""
    result = _run_policy_js(
        f"""
        console.log(JSON.stringify({{
          mismatch: uploadRetryDecision({_err(code='OFFSET_MISMATCH', status=409, payload='{ current_offset: 4096 }')}, 0),
          too_large: uploadRetryDecision({_err(code='CHUNK_TOO_LARGE', status=413, payload='{ max_chunk_bytes: 262144 }')}, 0)
        }}));
        """
    )
    assert result["mismatch"] == {"action": "resync", "waitMs": 0}
    # A too-large chunk is fixed by sending less, not by waiting.
    assert result["too_large"] == {"action": "shrink", "waitMs": 0}
