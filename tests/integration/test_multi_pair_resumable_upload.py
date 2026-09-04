"""The 25 required multi-content-set resumable-upload scenarios (V1.1 Phase 2).

One test per numbered requirement, in order, so a failure names the behaviour
that regressed rather than a helper. The invariant every one of these defends
is one sentence longer than Phase 1's: **every byte the server has confirmed
stays confirmed, for every content set, even when a different content set
fails.** A creator with three content sets must never re-upload set 1 because
set 3 broke.

Media fixtures are duplicated from tests/integration/test_resumable_upload.py
for the same documented reason that file gives: a stray global site-packages
`tests` package shadows dotted `tests.xxx` imports in this environment.

Test 7 executes the REAL shipped client policy code in Node (the DOM-free
block in templates/user/user_create_project.html), exactly as Phase 1's
scenarios do - asserting on template strings would prove the code is present,
not that it decides correctly.
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
def _jpeg_bytes(width=640, height=480, shade=120):
    out = BytesIO()
    Image.new("RGB", (width, height), (shade, 80, 40)).save(out, format="JPEG")
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


def _allow_pairs(app_module, user, max_pairs=5, projects=10):
    """Make the plan's per-project pair allowance explicit rather than
    inherited from whatever the trial seed happens to be, so these tests assert
    multi-set behaviour and not a seed value."""
    plan = user.subscription_plan
    plan.max_pairs_per_project = max_pairs
    user.subscribed_project_limit = projects
    app_module.db.session.commit()
    return plan


def _create_set(client, image_bytes, video_bytes, purpose="project_content_set", **extra):
    payload = {
        "image_size": len(image_bytes),
        "video_size": len(video_bytes),
        "project_name": extra.pop("project_name", "Three Set Story"),
        "purpose": purpose,
    }
    payload.update(extra)
    resp = client.post("/api/uploads/sessions", json=payload)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["session"]["id"]


def _send_chunk(client, session_id, offset, data):
    return client.post(
        f"/api/uploads/sessions/{session_id}/chunk",
        data=data,
        headers={"X-Chunk-Offset": str(offset)},
        content_type="application/octet-stream",
    )


def _status(client, session_id):
    return client.get(f"/api/uploads/sessions/{session_id}")


def _upload_all(client, session_id, blob, chunk=4096):
    """Send every byte of one content set, in order."""
    offset = 0
    while offset < len(blob):
        resp = _send_chunk(client, session_id, offset, blob[offset:offset + chunk])
        assert resp.status_code == 200, resp.get_json()
        offset = resp.get_json()["current_offset"]
    return offset


def _upload_partial(client, session_id, blob, upto, chunk=4096):
    # Clamped so a caller asking for "about half" of a small fixture cannot
    # walk past the end and send an empty chunk.
    upto = max(1, min(upto, len(blob) - 1))
    offset = 0
    while offset < upto:
        end = min(offset + chunk, upto)
        resp = _send_chunk(client, session_id, offset, blob[offset:end])
        assert resp.status_code == 200, resp.get_json()
        offset = resp.get_json()["current_offset"]
    return offset


def _finalize_project(client, session_ids):
    return client.post("/api/uploads/projects/finalize", json={"session_ids": list(session_ids)})


def _finalize_one(client, session_id):
    return client.post(f"/api/uploads/sessions/{session_id}/finalize")


def _temp_bytes(app_module, session_id):
    """The on-disk assembled bytes - ground truth that no assertion about
    `current_offset` alone can substitute for."""
    row = app_module.UploadSession.query.get(session_id)
    path = app_module._upload_session_temp_path(row.storage_token)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return fh.read()


def _make_sets(count, video_frames=5):
    """`count` distinct content sets, each a (marker_bytes, video_bytes,
    combined) triple. Distinct marker shades so a mixed-up ordering shows up as
    a byte difference rather than passing by accident."""
    sets = []
    for index in range(count):
        image = _jpeg_bytes(shade=100 + index * 30)
        video = _mp4_bytes(frames=video_frames + index)
        sets.append((image, video, image + video))
    return sets


def _upload_group(client, app_module, count=2, project_name="Three Set Story"):
    """`count` content sets, all fully uploaded, none finalized."""
    sets = _make_sets(count)
    ids = []
    for image, video, combined in sets:
        session_id = _create_set(client, image, video, project_name=project_name)
        _upload_all(client, session_id, combined)
        ids.append(session_id)
    return ids, sets


# ---------------------------------------------------------------------
# Real-JavaScript harness (identical to Phase 1's, so the same shipped
# policy block is what gets executed)
# ---------------------------------------------------------------------
def _upload_policy_js():
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        source = fh.read()
    block = source[source.index(JS_BLOCK_START):source.index(JS_BLOCK_END)]
    assert "fingerprintsMatch" in block, "policy block markers drifted"
    return block


def _run_policy_js(script_body):
    if NODE is None:  # pragma: no cover - environment guard
        pytest.skip("node is required to execute the shipped uploader policy code")
    harness = (
        "const navigator = { connection: undefined };\n"
        "const window = { crypto: undefined };\n"
        + _upload_policy_js()
        + "\n(async () => {\n" + script_body
        + "\n})().catch(err => { console.error(String(err && err.stack || err)); process.exit(1); });\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "policy_check.mjs")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(harness)
        proc = subprocess.run([NODE, path], capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _matcher_js():
    """The REAL storedSessionMatchesFiles from the template, called the way the
    multi-set client calls it: a per-set record spread over the project-level
    record. Pulled out of the shipped file rather than restated here."""
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        source = fh.read()
    start = source.index("function storedSessionMatchesFiles(")
    end = source.index("function sequentialUploadSlice(")
    return source[start:end]


# =====================================================================
# 1. Two-set normal creation
# =====================================================================
def test_01_two_set_normal_creation(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)
    ids, _sets = _upload_group(client, app_module, count=2)

    resp = _finalize_project(client, ids)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["success"] is True

    assert app_module.Project.query.count() == 1
    project = app_module.Project.query.one()
    assert app_module.ProjectPair.query.filter_by(project_id=project.id).count() == 2
    assert [row.status for row in app_module.UploadSession.query.all()] == ["completed", "completed"]
    assert {row.project_id for row in app_module.UploadSession.query.all()} == {project.id}
    # One project-quota unit for the whole project, not one per content set.
    app_module.db.session.refresh(login_user)
    assert (login_user.projects_used or 0) == 1


# =====================================================================
# 2. Three-set normal creation
# =====================================================================
def test_02_three_set_normal_creation(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)
    ids, _sets = _upload_group(client, app_module, count=3)

    assert _finalize_project(client, ids).status_code == 200
    project = app_module.Project.query.one()
    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).order_by(
        app_module.ProjectPair.pair_index
    ).all()
    assert [p.pair_index for p in pairs] == [0, 1, 2]
    assert all(p.video_filename for p in pairs)
    app_module.db.session.refresh(login_user)
    assert (login_user.projects_used or 0) == 1


# =====================================================================
# 3. Set 1 complete, set 2 interrupted, then resumed
# =====================================================================
def test_03_set1_complete_set2_interrupted_then_resumes(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)
    sets = _make_sets(2)
    first = _create_set(client, *sets[0][:2])
    _upload_all(client, first, sets[0][2])
    second = _create_set(client, *sets[1][:2])
    partial = _upload_partial(client, second, sets[1][2], upto=len(sets[1][2]) // 2)
    assert 0 < partial < len(sets[1][2])
    set1_bytes = _temp_bytes(app_module, first)

    # The group is not finalizable yet, and the refusal carries every set's
    # authoritative offset so the client resumes only what is short.
    resp = _finalize_project(client, [first, second])
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["code"] == "INCOMPLETE_UPLOAD"
    states = {row["set_index"]: row for row in body["sessions"]}
    assert states[0]["set_state"] == "uploaded"
    assert states[1]["set_state"] == "uploading"
    assert states[1]["current_offset"] == partial

    # Resume set 2 from the server's offset only.
    offset = states[1]["current_offset"]
    while offset < len(sets[1][2]):
        r = _send_chunk(client, second, offset, sets[1][2][offset:offset + 4096])
        assert r.status_code == 200
        offset = r.get_json()["current_offset"]

    assert _finalize_project(client, [first, second]).status_code == 200
    assert app_module.ProjectPair.query.count() == 2
    # Set 1's bytes were never re-sent: they are byte-identical to what the
    # server had confirmed before the interruption.
    assert set1_bytes == sets[0][2]


# =====================================================================
# 4. Refresh after set 1 completed
# =====================================================================
def test_04_refresh_after_set1_complete_recovers_group_state(client, app_module, login_user):
    _allow_pairs(app_module, login_user)
    sets = _make_sets(3)
    ids = [_create_set(client, *s[:2]) for s in sets]
    _upload_all(client, ids[0], sets[0][2])

    # A refresh cannot restore File handles, but it CAN re-ask the server about
    # every set. Server state is the only state that matters here.
    observed = []
    for session_id in ids:
        resp = _status(client, session_id)
        assert resp.status_code == 200
        observed.append(resp.get_json()["session"])
    assert [row["set_state"] for row in observed] == ["uploaded", "pending", "pending"]
    assert observed[0]["current_offset"] == len(sets[0][2])
    assert observed[0]["can_upload_chunks"] is False
    assert observed[1]["can_upload_chunks"] is True


# =====================================================================
# 5. Refresh midway through set 2
# =====================================================================
def test_05_refresh_midway_set2_resumes_from_server_offset(client, app_module, login_user):
    _allow_pairs(app_module, login_user)
    sets = _make_sets(3)
    ids = [_create_set(client, *s[:2]) for s in sets]
    _upload_all(client, ids[0], sets[0][2])
    partial = _upload_partial(client, ids[1], sets[1][2], upto=len(sets[1][2]) // 2)

    after_refresh = [_status(client, i).get_json()["session"] for i in ids]
    assert after_refresh[0]["set_state"] == "uploaded"
    assert after_refresh[1]["set_state"] == "uploading"
    assert after_refresh[1]["current_offset"] == partial
    assert 0 < partial < len(sets[1][2])
    assert after_refresh[2]["set_state"] == "pending"

    # Resuming from the recovered offset costs zero retransmitted bytes.
    resp = _send_chunk(client, ids[1], partial, sets[1][2][partial:partial + 4096])
    assert resp.status_code == 200
    assert resp.get_json()["current_offset"] == partial + len(sets[1][2][partial:partial + 4096])


# =====================================================================
# 6. Server state overrides local belief, for every set
# =====================================================================
def test_06_server_state_overrides_local_belief_for_every_set(client, app_module, login_user):
    _allow_pairs(app_module, login_user)
    sets = _make_sets(2)
    ids = [_create_set(client, *s[:2]) for s in sets]
    _upload_all(client, ids[0], sets[0][2])
    _upload_partial(client, ids[1], sets[1][2], upto=4096)

    # A client that believes set 2 is finished is simply wrong, and the server
    # says so rather than accepting the claim.
    resp = _finalize_project(client, ids)
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "INCOMPLETE_UPLOAD"
    truth = {row["set_index"]: row["current_offset"] for row in resp.get_json()["sessions"]}
    assert truth[1] == 4096
    assert app_module.Project.query.count() == 0

    # And a chunk sent at the believed-but-wrong offset is rejected with the
    # authoritative one inline.
    resp = _send_chunk(client, ids[1], len(sets[1][2]) - 10, sets[1][2][-10:])
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "OFFSET_MISMATCH"
    assert resp.get_json()["current_offset"] == 4096


# =====================================================================
# 7. Fingerprint mismatch affects exactly one set
# =====================================================================
def test_07_fingerprint_mismatch_rejects_only_the_changed_set():
    result = _run_policy_js(
        _matcher_js()
        + """
      const project = {
        version: 3, projectName: 'Three Set Story',
        experience_type: 'image_video', playback_mode: 'tracked_overlay'
      };
      const fp = (head, tail) => ({ name: 'clip.mp4', size: 100, lastModified: 7, headSha256: head, tailSha256: tail });
      const record = (videoFp) => ({
        imageName: 'marker.jpg', imageSize: 10, videoName: 'clip.mp4', videoSize: 100,
        videoLastModified: 7, imageFingerprint: null, videoFingerprint: videoFp
      });
      const marker = { name: 'marker.jpg', size: 10 };
      const video = { name: 'clip.mp4', size: 100, lastModified: 7 };
      const payload = { experience_type: 'image_video', playback_mode: 'tracked_overlay' };
      const same = fp('aa', 'bb');
      const different = fp('cc', 'dd');
      // Three sets; only the middle one's file was replaced.
      const results = [same, different, same].map(stored => storedSessionMatchesFiles(
        { ...project, ...record(stored) }, marker, video, 'Three Set Story', payload,
        { image: null, video: same }
      ));
      console.log(JSON.stringify({ results }));
    """
    )
    assert result["results"] == [True, False, True], result


# =====================================================================
# 8. A completed set is never re-uploaded
# =====================================================================
def test_08_completed_set_is_never_reuploaded(client, app_module, login_user):
    _allow_pairs(app_module, login_user)
    sets = _make_sets(2)
    ids = [_create_set(client, *s[:2]) for s in sets]
    _upload_all(client, ids[0], sets[0][2])
    before = _temp_bytes(app_module, ids[0])

    # A client re-sending set 1 from zero is answered as a duplicate replay, not
    # an append. The offset does not move and not one byte is added.
    resp = _send_chunk(client, ids[0], 0, sets[0][2][:4096])
    assert resp.status_code == 200
    assert resp.get_json()["note"] == "duplicate_chunk_ignored"
    assert resp.get_json()["current_offset"] == len(sets[0][2])
    assert _temp_bytes(app_module, ids[0]) == before
    assert _status(client, ids[0]).get_json()["session"]["can_upload_chunks"] is False


# =====================================================================
# 9. Duplicate chunk on set 2
# =====================================================================
def test_09_duplicate_chunk_on_set_two_is_idempotent(client, app_module, login_user):
    _allow_pairs(app_module, login_user)
    sets = _make_sets(2)
    ids = [_create_set(client, *s[:2]) for s in sets]
    _upload_all(client, ids[0], sets[0][2])
    partial = _upload_partial(client, ids[1], sets[1][2], upto=len(sets[1][2]) // 2)
    assert 0 < partial < len(sets[1][2])
    replay = min(4096, partial)

    for _ in range(4):
        resp = _send_chunk(client, ids[1], 0, sets[1][2][:replay])
        assert resp.status_code == 200
        assert resp.get_json()["note"] == "duplicate_chunk_ignored"
        assert resp.get_json()["current_offset"] == partial
    assert _temp_bytes(app_module, ids[1]) == sets[1][2][:partial]
    # And set 1 was not disturbed by any of it.
    assert _temp_bytes(app_module, ids[0]) == sets[0][2]


# =====================================================================
# 10. Duplicate finalize involving set 2
# =====================================================================
def test_10_duplicate_finalize_of_the_group_creates_one_project(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)
    ids, _sets = _upload_group(client, app_module, count=2)

    first = _finalize_project(client, ids)
    assert first.status_code == 200
    project_id = first.get_json()["session"]["project_id"]

    second = _finalize_project(client, ids)
    assert second.status_code == 200
    assert second.get_json()["recovered_existing_completion"] is True
    assert second.get_json()["session"]["project_id"] == project_id
    assert app_module.Project.query.count() == 1
    assert app_module.ProjectPair.query.count() == 2


def test_10b_a_content_set_cannot_be_finalized_as_its_own_project(client, app_module, login_user):
    """The hazard this closes: finalizing content set 2 of 3 on its own would
    silently produce a stray one-pair project and burn a project-quota unit on
    it. The purpose recorded at session creation is what makes that request
    refusable at all."""
    _allow_pairs(app_module, login_user)
    ids, _sets = _upload_group(client, app_module, count=2)
    resp = _finalize_one(client, ids[1])
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "GROUP_FINALIZE_REQUIRED"
    assert app_module.Project.query.count() == 0
    # Still finalizable the correct way, with nothing lost.
    assert _finalize_project(client, ids).status_code in (200, 502)


# =====================================================================
# 11. Duplicate overall project completion
# =====================================================================
def test_11_triple_project_finalize_produces_one_project_and_one_job(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)
    ids, _sets = _upload_group(client, app_module, count=3)

    codes = []
    for _ in range(3):
        resp = _finalize_project(client, ids)
        codes.append(resp.status_code)
    assert codes == [200, 200, 200]
    project = app_module.Project.query.one()
    assert app_module.ProjectPair.query.count() == 3

    # Fast Video Phase 2: every PairMedia created by this finalize gets its
    # own optimize_pair_media job, additive to (not instead of) the one
    # project-level feature-extraction job - a raw total ProcessingJob count
    # is no longer "how many times did finalize enqueue processing".
    feature_jobs = app_module.ProcessingJob.query.filter_by(
        project_id=project.id, job_type="process_project_pairs"
    ).all()
    assert len(feature_jobs) == 1  # one enqueue attempt, never duplicated across the 3 replays

    media_count = app_module.PairMedia.query.join(app_module.ProjectPair).filter(
        app_module.ProjectPair.project_id == project.id
    ).count()
    optimization_jobs = app_module.ProcessingJob.query.filter_by(
        project_id=project.id, job_type="optimize_pair_media"
    ).all()
    assert len(optimization_jobs) == media_count  # exactly one per PairMedia
    assert len({j.pair_media_id for j in optimization_jobs}) == len(optimization_jobs)  # no duplicate per PairMedia

    app_module.db.session.refresh(login_user)
    assert (login_user.projects_used or 0) == 1


# =====================================================================
# 12. Double-clicked Create
# =====================================================================
def test_12_double_click_create_finalizes_exactly_once(client, app_module, login_user, monkeypatch):
    """The atomic all-N-or-none claim is what makes this safe. Simulated by
    pre-claiming the rows the way a racing request would, then letting the
    second request discover it lost."""
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)
    ids, _sets = _upload_group(client, app_module, count=2)

    # A racing request has claimed the group and is mid-finalize.
    app_module.UploadSession.query.filter(app_module.UploadSession.id.in_(ids)).update(
        {app_module.UploadSession.status: "finalizing"}, synchronize_session=False
    )
    app_module.db.session.commit()

    resp = _finalize_project(client, ids)
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "FINALIZE_IN_PROGRESS"
    assert app_module.Project.query.count() == 0

    # The winner finishes; exactly one project exists at the end.
    app_module.UploadSession.query.filter(app_module.UploadSession.id.in_(ids)).update(
        {app_module.UploadSession.status: "active"}, synchronize_session=False
    )
    app_module.db.session.commit()
    assert _finalize_project(client, ids).status_code == 200
    assert app_module.Project.query.count() == 1
    assert app_module.ProjectPair.query.count() == 2


# =====================================================================
# 13. Connection loss between sets
# =====================================================================
def test_13_connection_loss_between_sets_preserves_the_finished_one(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)
    sets = _make_sets(2)
    first = _create_set(client, *sets[0][:2])
    _upload_all(client, first, sets[0][2])
    # The link dies before set 2 sends anything at all.
    second = _create_set(client, *sets[1][:2])

    resp = _finalize_project(client, [first, second])
    assert resp.status_code == 409
    states = {row["set_index"]: row["set_state"] for row in resp.get_json()["sessions"]}
    assert states == {0: "uploaded", 1: "pending"}
    assert _temp_bytes(app_module, first) == sets[0][2]

    _upload_all(client, second, sets[1][2])
    assert _finalize_project(client, [first, second]).status_code == 200
    assert app_module.ProjectPair.query.count() == 2


# =====================================================================
# 14. Connection loss during set 3
# =====================================================================
def test_14_connection_loss_during_set3_leaves_sets_one_and_two_intact(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)
    sets = _make_sets(3)
    ids = [_create_set(client, *s[:2]) for s in sets]
    _upload_all(client, ids[0], sets[0][2])
    _upload_all(client, ids[1], sets[1][2])
    partial = _upload_partial(client, ids[2], sets[2][2], upto=4096)

    resp = _finalize_project(client, ids)
    assert resp.status_code == 409
    states = {row["set_index"]: row for row in resp.get_json()["sessions"]}
    assert states[0]["set_state"] == "uploaded"
    assert states[1]["set_state"] == "uploaded"
    assert states[2]["current_offset"] == partial
    assert _temp_bytes(app_module, ids[0]) == sets[0][2]
    assert _temp_bytes(app_module, ids[1]) == sets[1][2]

    offset = partial
    while offset < len(sets[2][2]):
        r = _send_chunk(client, ids[2], offset, sets[2][2][offset:offset + 4096])
        offset = r.get_json()["current_offset"]
    assert _finalize_project(client, ids).status_code == 200
    assert app_module.ProjectPair.query.count() == 3


# =====================================================================
# 15. A failed set does not destroy the sets that succeeded
# =====================================================================
def test_15_failed_set_does_not_destroy_prior_completed_sets(client, app_module, login_user, monkeypatch):
    """The headline requirement. Set 3's video is not a video; the finalize must
    reject THAT set and hand sets 1 and 2 back their confirmed bytes."""
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)
    good = _make_sets(2)
    bad_image = _jpeg_bytes(shade=200)
    bad_video = b"definitely not an mp4" * 40

    ids = [_create_set(client, *s[:2]) for s in good]
    _upload_all(client, ids[0], good[0][2])
    _upload_all(client, ids[1], good[1][2])
    third = _create_set(client, bad_image, bad_video)
    _upload_all(client, third, bad_image + bad_video)

    resp = _finalize_project(client, ids + [third])
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["code"] == "VIDEO_VALIDATION_FAILED"
    assert body["failed_set_index"] == 2
    assert body["failed_session_id"] == third

    # Only the offender is terminal; the others are resumable again and still
    # hold every byte they had.
    rows = {row.id: row for row in app_module.UploadSession.query.all()}
    assert rows[third].status == "failed"
    assert rows[ids[0]].status == "active"
    assert rows[ids[1]].status == "active"
    assert _temp_bytes(app_module, ids[0]) == good[0][2]
    assert _temp_bytes(app_module, ids[1]) == good[1][2]
    assert _temp_bytes(app_module, third) is None
    assert app_module.Project.query.count() == 0
    app_module.db.session.refresh(login_user)
    assert (login_user.projects_used or 0) == 0

    # Replacing ONE file finishes the project; sets 1 and 2 upload nothing more.
    replacement_image = _jpeg_bytes(shade=210)
    replacement_video = _mp4_bytes(frames=9)
    fourth = _create_set(client, replacement_image, replacement_video)
    _upload_all(client, fourth, replacement_image + replacement_video)
    assert _finalize_project(client, ids + [fourth]).status_code == 200
    assert app_module.ProjectPair.query.count() == 3


# =====================================================================
# 16. Quota and storage accounting
# =====================================================================
def test_16_quota_and_storage_accounting_is_correct_for_a_group(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)
    before_used, _allowance = app_module.account_storage_state(login_user)
    ids, sets = _upload_group(client, app_module, count=3)

    # A paused / not-yet-finalized upload costs no quota and no storage. This is
    # what makes a longer recoverable-pause window safe to offer.
    app_module.db.session.refresh(login_user)
    assert (login_user.projects_used or 0) == 0
    mid_used, _allowance = app_module.account_storage_state(login_user)
    assert mid_used == before_used

    assert _finalize_project(client, ids).status_code == 200
    app_module.db.session.refresh(login_user)
    # ONE project unit for three content sets.
    assert (login_user.projects_used or 0) == 1
    after_used, _allowance = app_module.account_storage_state(login_user)
    assert after_used > mid_used
    # Every retained byte is ledgered exactly once: an image and a video per set.
    ledger = app_module.MediaObject.query.all()
    assert len(ledger) == 6
    assert sum(row.size_bytes for row in ledger) == after_used - before_used


def test_16b_pair_limit_is_refused_before_anything_is_claimed(client, app_module, login_user):
    _allow_pairs(app_module, login_user, max_pairs=2)
    ids, _sets = _upload_group(client, app_module, count=3)
    resp = _finalize_project(client, ids)
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "PAIR_LIMIT_REACHED"
    # Refused BEFORE the claim: every set is still active and still holds its
    # bytes, so raising the plan and pressing Resume works.
    assert [row.status for row in app_module.UploadSession.query.all()] == ["active"] * 3
    assert app_module.Project.query.count() == 0


# =====================================================================
# 17. Content-set order is preserved
# =====================================================================
def test_17_content_set_order_is_preserved_as_pair_index(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)
    sets = _make_sets(3)
    ids = [_create_set(client, *s[:2]) for s in sets]
    # Uploaded OUT of order on purpose: the request's order is what defines the
    # content-set order, not the order bytes happened to arrive.
    for index in (2, 0, 1):
        _upload_all(client, ids[index], sets[index][2])

    assert _finalize_project(client, ids).status_code == 200
    pairs = app_module.ProjectPair.query.order_by(app_module.ProjectPair.pair_index).all()
    assert [p.pair_index for p in pairs] == [0, 1, 2]
    sessions = {row.id: row for row in app_module.UploadSession.query.all()}
    assert [sessions[i].pair_id for i in ids] == [p.id for p in pairs]
    # The declared per-set sizes land on the pair the client meant.
    assert [p.video_size for p in pairs] == [len(s[1]) for s in sets]


# =====================================================================
# 18. Exact expected pair count
# =====================================================================
@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_18_project_has_exactly_the_expected_pair_count(client, app_module, login_user, monkeypatch, count):
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)
    ids, _sets = _upload_group(client, app_module, count=count)
    assert _finalize_project(client, ids).status_code == 200
    assert app_module.Project.query.count() == 1
    assert app_module.ProjectPair.query.count() == count


# =====================================================================
# 19. No orphan media rows
# =====================================================================
def test_19_no_orphan_media_rows_on_success_or_failure(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)

    # Failure first: a rejected group must leave the ledger completely empty.
    good = _make_sets(1)
    bad_image = _jpeg_bytes(shade=90)
    ids = [_create_set(client, *good[0][:2])]
    _upload_all(client, ids[0], good[0][2])
    bad = _create_set(client, bad_image, b"not a video" * 50)
    _upload_all(client, bad, bad_image + b"not a video" * 50)
    assert _finalize_project(client, ids + [bad]).status_code == 422
    assert app_module.MediaObject.query.count() == 0

    # Success: exactly one image row and one video row per pair, each bound to
    # its own pair, none dangling. Deliberately a DIFFERENT shade from `good`
    # above (not another _make_sets(1), which always regenerates shade=100 for
    # its single set) - this is genuinely a second, distinct target, and the
    # new exact-duplicate-target guard correctly rejects two targets sharing
    # byte-identical image content within one project.
    replacement_image = _jpeg_bytes(shade=170)
    replacement_video = _mp4_bytes(frames=6)
    replacement = (replacement_image, replacement_video, replacement_image + replacement_video)
    second = _create_set(client, *replacement[:2])
    _upload_all(client, second, replacement[2])
    assert _finalize_project(client, ids + [second]).status_code == 200
    project = app_module.Project.query.one()
    pair_ids = {p.id for p in app_module.ProjectPair.query.filter_by(project_id=project.id)}
    ledger = app_module.MediaObject.query.all()
    assert len(ledger) == 4
    assert all(row.pair_id in pair_ids for row in ledger)
    assert all(row.project_id == project.id for row in ledger)


# =====================================================================
# 20. No duplicate processing jobs
# =====================================================================
def test_20_no_duplicate_processing_jobs_for_a_multi_set_project(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)
    calls = []
    real = app_module._schedule_project_pair_processing

    def counting(project_id, *args, **kwargs):
        calls.append(project_id)
        return real(project_id, *args, **kwargs)

    monkeypatch.setattr(app_module, "_schedule_project_pair_processing", counting)
    ids, _sets = _upload_group(client, app_module, count=3)
    assert _finalize_project(client, ids).status_code == 200
    for _ in range(3):
        assert _finalize_project(client, ids).status_code == 200

    # ONE enqueue attempt for three content sets, and no second job however
    # many times finalize is replayed.
    assert len(calls) == 1
    project = app_module.Project.query.one()

    # Fast Video Phase 2: one optimize_pair_media job per PairMedia is
    # additive to this project-level feature-extraction job, not a sign the
    # dedupe broke - assert each job_type's own no-duplicate invariant
    # instead of a raw total.
    feature_jobs = app_module.ProcessingJob.query.filter_by(
        project_id=project.id, job_type="process_project_pairs"
    ).all()
    assert len(feature_jobs) == 1  # no duplicate across the 4 finalize calls above

    media_count = app_module.PairMedia.query.join(app_module.ProjectPair).filter(
        app_module.ProjectPair.project_id == project.id
    ).count()
    optimization_jobs = app_module.ProcessingJob.query.filter_by(
        project_id=project.id, job_type="optimize_pair_media"
    ).all()
    assert len(optimization_jobs) == media_count
    assert len({j.pair_media_id for j in optimization_jobs}) == len(optimization_jobs)


def test_20b_enqueue_failure_parks_every_set_and_retries_only_the_enqueue(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)
    ids, _sets = _upload_group(client, app_module, count=2)
    monkeypatch.setattr(app_module, "_schedule_project_pair_processing", lambda *a, **k: None)

    resp = _finalize_project(client, ids)
    assert resp.status_code == 502
    assert resp.get_json()["code"] == "QUEUE_ENQUEUE_FAILED"
    rows = app_module.UploadSession.query.all()
    assert {row.status for row in rows} == {"assembled"}
    assert len({row.project_id for row in rows}) == 1
    assert app_module.ProjectPair.query.count() == 2

    # Retrying finalize retries ONLY the enqueue: no second project, no second
    # quota unit, no re-validation.
    monkeypatch.setattr(app_module, "_schedule_project_pair_processing", real_or_stub := (lambda *a, **k: type("J", (), {"id": 1})()))
    assert _finalize_project(client, ids).status_code == 200
    assert app_module.Project.query.count() == 1
    assert app_module.ProjectPair.query.count() == 2
    app_module.db.session.refresh(login_user)
    assert (login_user.projects_used or 0) == 1


# =====================================================================
# 21. A paused session survives the configured inactivity period
# =====================================================================
def test_21_paused_session_survives_the_configured_inactivity_period(client, app_module, login_user):
    """The Phase 1 gap. The abandoned-stale window used to default to 120
    minutes while the headline TTL said 1440, and the SHORTER of the two is
    what actually bounds a paused upload - so 'resume when you're ready' was
    honest for two hours, not a day."""
    assert app_module.UPLOAD_SESSION_ABANDONED_STALE_MINUTES >= app_module.UPLOAD_SESSION_TTL_MINUTES
    assert app_module.UPLOAD_SESSION_TTL_MINUTES >= 24 * 60

    _allow_pairs(app_module, login_user)
    sets = _make_sets(2)
    ids = [_create_set(client, *s[:2]) for s in sets]
    _upload_all(client, ids[0], sets[0][2])
    _upload_partial(client, ids[1], sets[1][2], upto=4096)

    # Pause for 20 hours: inside the window, so both sets survive.
    twenty_hours_ago = app_module.get_utc_now() - timedelta(hours=20)
    app_module.UploadSession.query.filter(app_module.UploadSession.id.in_(ids)).update(
        {app_module.UploadSession.updated_at: twenty_hours_ago}, synchronize_session=False
    )
    app_module.db.session.commit()
    runner = app_module.app.test_cli_runner()
    result = runner.invoke(args=["cleanup-upload-sessions", "--apply"])
    assert result.exit_code == 0
    assert "Expired: 0" in result.output
    assert [row.status for row in app_module.UploadSession.query.all()] == ["active", "active"]
    assert _temp_bytes(app_module, ids[0]) == sets[0][2]

    # A status read is proof the creator is still there and slides the deadline,
    # which is what keeps a set that finished early alive while its siblings
    # crawl.
    before = app_module.UploadSession.query.get(ids[0]).expires_at
    app_module.UploadSession.query.filter_by(id=ids[0]).update(
        {app_module.UploadSession.expires_at: app_module.get_utc_now() + timedelta(minutes=5)},
        synchronize_session=False,
    )
    app_module.db.session.commit()
    assert _status(client, ids[0]).status_code == 200
    after = app_module.UploadSession.query.get(ids[0]).expires_at
    assert after > app_module.get_utc_now() + timedelta(hours=20)
    assert before is not None


# =====================================================================
# 22. A genuinely abandoned session is still cleaned up
# =====================================================================
def test_22_genuinely_abandoned_session_is_still_cleaned_up(client, app_module, login_user):
    _allow_pairs(app_module, login_user)
    sets = _make_sets(2)
    ids = [_create_set(client, *s[:2]) for s in sets]
    _upload_partial(client, ids[0], sets[0][2], upto=4096)
    temp_paths = [
        app_module._upload_session_temp_path(app_module.UploadSession.query.get(i).storage_token)
        for i in ids
    ]
    assert all(os.path.exists(p) for p in temp_paths)

    # Truly abandoned: no activity for longer than the window, and the deadline
    # itself has passed.
    stale = app_module.get_utc_now() - timedelta(minutes=app_module.UPLOAD_SESSION_ABANDONED_STALE_MINUTES + 60)
    app_module.UploadSession.query.filter(app_module.UploadSession.id.in_(ids)).update(
        {app_module.UploadSession.updated_at: stale, app_module.UploadSession.expires_at: stale},
        synchronize_session=False,
    )
    app_module.db.session.commit()

    runner = app_module.app.test_cli_runner()
    assert "Expired: 2" in runner.invoke(args=["cleanup-upload-sessions", "--apply"]).output
    rows = app_module.UploadSession.query.all()
    assert {row.status for row in rows} == {"expired"}
    assert {row.failure_code for row in rows} == {"SESSION_TTL_EXPIRED"}
    assert not any(os.path.exists(p) for p in temp_paths)
    # An expired set is refused cleanly rather than failing obscurely.
    resp = _finalize_project(client, ids)
    assert resp.status_code == 409
    assert resp.get_json()["code"] in {"SESSION_EXPIRED", "INCOMPLETE_UPLOAD"}
    # And a status read never resurrects an expired session.
    assert _status(client, ids[0]).get_json()["session"]["set_state"] == "failed_requires_action"


# =====================================================================
# 23. Explicit cancel
# =====================================================================
def test_23_explicit_cancel_releases_only_what_was_cancelled(client, app_module, login_user):
    _allow_pairs(app_module, login_user)
    sets = _make_sets(3)
    ids = [_create_set(client, *s[:2]) for s in sets]
    for index, (_i, _v, combined) in enumerate(sets):
        _upload_all(client, ids[index], combined)

    # Cancelling ONE content set frees that set's bytes and nothing else.
    assert client.post(f"/api/uploads/sessions/{ids[1]}/cancel").status_code == 200
    assert app_module.UploadSession.query.get(ids[1]).status == "cancelled"
    assert _temp_bytes(app_module, ids[1]) is None
    assert _temp_bytes(app_module, ids[0]) == sets[0][2]
    assert _temp_bytes(app_module, ids[2]) == sets[2][2]

    # The group is no longer finalizable, and no quota was ever charged.
    resp = _finalize_project(client, ids)
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "INCOMPLETE_UPLOAD"
    app_module.db.session.refresh(login_user)
    assert (login_user.projects_used or 0) == 0

    # Cancelling the whole flow releases every remaining set.
    for session_id in (ids[0], ids[2]):
        assert client.post(f"/api/uploads/sessions/{session_id}/cancel").status_code == 200
    assert all(_temp_bytes(app_module, i) is None for i in ids)
    assert app_module.Project.query.count() == 0


# =====================================================================
# 24. Direct QR is unaffected
# =====================================================================
def test_24_direct_qr_multi_set_is_unaffected(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)

    # Direct QR still refuses marker bytes outright.
    bad = client.post("/api/uploads/sessions", json={
        "image_size": 10, "video_size": 100,
        "experience_type": "direct_qr", "playback_mode": "direct",
        "purpose": "project_content_set",
    })
    assert bad.status_code == 400
    assert bad.get_json()["code"] == "INVALID_SIZE"

    videos = [_mp4_bytes(frames=5), _mp4_bytes(frames=6)]
    ids = []
    for video in videos:
        resp = client.post("/api/uploads/sessions", json={
            "image_size": 0, "video_size": len(video),
            "experience_type": "direct_qr", "playback_mode": "direct",
            "project_name": "Direct Pair", "purpose": "project_content_set",
        })
        assert resp.status_code == 201, resp.get_json()
        session_id = resp.get_json()["session"]["id"]
        _upload_all(client, session_id, video)
        ids.append(session_id)

    assert _finalize_project(client, ids).status_code == 200
    project = app_module.Project.query.one()
    assert project.experience_type == "direct_qr"
    pairs = app_module.ProjectPair.query.order_by(app_module.ProjectPair.pair_index).all()
    assert [p.pair_index for p in pairs] == [0, 1]
    assert all(p.image_filename is None for p in pairs)
    assert all(p.is_processed for p in pairs)
    assert all(p.feature_extraction_status == "not_required" for p in pairs)

    # Direct QR needs no image-feature-extraction processing, so it must
    # enqueue ZERO process_project_pairs jobs - that invariant still holds.
    # It legitimately DOES still get one optimize_pair_media job per
    # PairMedia (Fast Video optimizes the served video regardless of
    # recognition mode), which a raw total-count assertion couldn't tell
    # apart from a real regression.
    assert app_module.ProcessingJob.query.filter_by(
        project_id=project.id, job_type="process_project_pairs"
    ).count() == 0

    media_count = app_module.PairMedia.query.join(app_module.ProjectPair).filter(
        app_module.ProjectPair.project_id == project.id
    ).count()
    optimization_jobs = app_module.ProcessingJob.query.filter_by(
        project_id=project.id, job_type="optimize_pair_media"
    ).all()
    assert len(optimization_jobs) == media_count
    assert len({j.pair_media_id for j in optimization_jobs}) == len(optimization_jobs)


def test_24b_mixed_experience_types_cannot_be_finalized_as_one_project(client, app_module, login_user):
    _allow_pairs(app_module, login_user)
    image, video, combined = _make_sets(1)[0]
    first = _create_set(client, image, video, project_name="Mixed")
    _upload_all(client, first, combined)
    direct_video = _mp4_bytes(frames=7)
    resp = client.post("/api/uploads/sessions", json={
        "image_size": 0, "video_size": len(direct_video),
        "experience_type": "direct_qr", "playback_mode": "direct",
        "project_name": "Mixed", "purpose": "project_content_set",
    })
    second = resp.get_json()["session"]["id"]
    _upload_all(client, second, direct_video)

    conflict = _finalize_project(client, [first, second])
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "CONTENT_SET_MISMATCH"
    assert app_module.Project.query.count() == 0


# =====================================================================
# 25. The single-pair resumable path is still green
# =====================================================================
def test_25_single_pair_resumable_path_remains_green(client, app_module, login_user, monkeypatch):
    """Regression check on the path Phase 1 built, through the UNCHANGED
    single-session finalize route. The full 66-test Phase 1 + Wave 5 suites are
    run separately (see the pass report); this is the end-to-end shape that
    would break first if generalizing the finalizer for N sets had disturbed
    N=1."""
    _patch_qr(app_module, monkeypatch)
    _allow_pairs(app_module, login_user)
    image, video, combined = _make_sets(1)[0]
    session_id = _create_set(client, image, video, purpose="project_pair", project_name="Single")
    _upload_all(client, session_id, combined)

    resp = _finalize_one(client, session_id)
    assert resp.status_code == 200, resp.get_json()
    session = resp.get_json()["session"]
    assert session["status"] == "completed"
    assert session["set_state"] == "complete"
    project = app_module.Project.query.one()
    assert project.id == session["project_id"]
    pair = app_module.ProjectPair.query.one()
    assert pair.pair_index == 0
    assert app_module.MediaObject.query.count() == 2
    app_module.db.session.refresh(login_user)
    assert (login_user.projects_used or 0) == 1
    # Replaying the finalize is still idempotent.
    assert _finalize_one(client, session_id).status_code == 409
    assert app_module.Project.query.count() == 1

    # And a single-pair project may equally be finalized through the group
    # route, which is what makes the two paths one implementation.
    second_image, second_video, second_combined = _make_sets(1)[0]
    second = _create_set(client, second_image, second_video, purpose="project_pair", project_name="Single Two")
    _upload_all(client, second, second_combined)
    assert _finalize_project(client, [second]).status_code == 200
    assert app_module.Project.query.count() == 2
