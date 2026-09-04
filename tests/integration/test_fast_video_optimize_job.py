"""Fast Video Phase 1: optimize_pair_media RQ job orchestration.

Real ffmpeg/ffprobe (skipped if unavailable, same convention as
test_fast_video_transcode.py), real files on disk under the isolated test
app's own VIDEOS_DIR, real PairMedia/ProcessingJob rows. SCANSTORY_TESTING
mode queues in "fake" mode, so every job here is run explicitly via
processing_operations.run_processing_job(job.id) - the same pattern already
used for the process_project_pairs job in this test suite.
"""
import os
import subprocess
from pathlib import Path

import pytest

import media_optimization as mo
from processing_operations import run_processing_job
from processing_queue import create_processing_job

FFMPEG = mo.resolve_ffmpeg_binary()
FFPROBE = mo.resolve_ffprobe_binary()
pytestmark = pytest.mark.skipif(
    not FFMPEG or not FFPROBE,
    reason="ffmpeg/ffprobe not resolvable (set SCANSTORY_FFMPEG_BINARY / SCANSTORY_FFPROBE_BINARY or add to PATH)",
)


def _make_real_video(path, width=960, height=720, duration=2, with_audio=True):
    args = [FFMPEG, "-y", "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={width}x{height}:rate=24"]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=1000:duration={duration}"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "0"]  # crf 0 (near-lossless): big enough to guarantee real shrinkage after re-encode
    args += ["-c:a", "aac"] if with_audio else ["-an"]
    args.append(path)
    subprocess.run(args, check=True, capture_output=True)


def _add_real_media(app_module, db_session, pair, filename, width=960, height=720, with_audio=True, **kwargs):
    path = os.path.join(app_module.VIDEOS_DIR, filename)
    _make_real_video(path, width=width, height=height, with_audio=with_audio)
    fields = dict(video_filename=filename, video_size=os.path.getsize(path), sort_order=1, is_default=False)
    fields.update(kwargs)
    media = app_module.PairMedia(pair_id=pair.id, **fields)
    db_session.add(media)
    db_session.commit()
    return media


def _run_job_for(app_module, db_session, media, expect_created=True):
    pair = app_module.ProjectPair.query.get(media.pair_id)
    job, created = create_processing_job(
        "optimize_pair_media",
        project_id=pair.project_id,
        pair_id=media.pair_id,
        pair_media_id=media.id,
        # A prior job for this same PairMedia may already be TERMINAL
        # (completed/failed) rather than active - "initial" reuses the same
        # deterministic idempotency_key regardless of terminal state and
        # would collide with it, exactly like process_project_pairs already
        # requires "reprocess" for a genuine rerun after completion.
        attempt_scope="reprocess",
    )
    assert created is expect_created
    result = run_processing_job(job.id)
    db_session.expire_all()
    return app_module.ProcessingJob.query.get(job.id), result


# ===========================================================================
# 7: job targets the correct PairMedia row
# ===========================================================================
def test_job_only_touches_its_own_pair_media_row(app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    media_a = _add_real_media(app_module, db_session, pair, f"{project.id}_0_a.mp4")
    media_b = _add_real_media(app_module, db_session, pair, f"{project.id}_0_b.mp4")

    _run_job_for(app_module, db_session, media_a)

    refreshed_a = app_module.PairMedia.query.get(media_a.id)
    refreshed_b = app_module.PairMedia.query.get(media_b.id)
    assert refreshed_a.optimization_status in {"ready", "failed"}  # attempted
    assert refreshed_b.optimization_status == "pending"  # untouched


# ===========================================================================
# 8/9: successful transcode -> ready; original untouched
# ===========================================================================
def test_successful_optimization_sets_ready_and_leaves_original_untouched(app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    media = _add_real_media(app_module, db_session, pair, f"{project.id}_0_orig.mp4")
    original_path = os.path.join(app_module.VIDEOS_DIR, media.video_filename)
    original_bytes = Path(original_path).read_bytes()

    job, result = _run_job_for(app_module, db_session, media)
    refreshed = app_module.PairMedia.query.get(media.id)

    assert result["ok"] is True
    assert refreshed.optimization_status == "ready"
    assert refreshed.optimization_error is None
    assert refreshed.optimized_video_filename
    assert refreshed.optimized_video_size
    assert refreshed.optimized_at is not None
    # video_filename (the original) is byte-for-byte untouched.
    assert refreshed.video_filename == media.video_filename
    assert Path(original_path).read_bytes() == original_bytes

    derivative_path = os.path.join(app_module.VIDEOS_DIR, refreshed.optimized_video_filename)
    assert os.path.exists(derivative_path)
    assert refreshed.optimized_video_filename == mo.derivative_filename(project.id, pair.id, media.id)


# ===========================================================================
# 15/16: failed ffmpeg -> failed; failure leaves original playable
# ===========================================================================
def test_corrupt_source_fails_safely_without_touching_original(app_module, db_session, project_with_pair, monkeypatch):
    project, pair = project_with_pair
    media = _add_real_media(app_module, db_session, pair, f"{project.id}_0_good.mp4")
    original_path = os.path.join(app_module.VIDEOS_DIR, media.video_filename)
    # Corrupt in place AFTER creating the row, so the "original" the job reads
    # is genuinely unplayable - the failure must not touch it further.
    Path(original_path).write_bytes(b"not a real video file")
    corrupted_bytes = Path(original_path).read_bytes()

    job, result = _run_job_for(app_module, db_session, media)
    refreshed = app_module.PairMedia.query.get(media.id)

    assert result["ok"] is False
    assert refreshed.optimization_status == "failed"
    assert refreshed.optimization_error
    assert len(refreshed.optimization_error) <= 500
    assert refreshed.optimized_video_filename is None
    # The (already-corrupt, but that's not this job's doing) original is
    # exactly as the job found it - never deleted, never rewritten.
    assert os.path.exists(original_path)
    assert Path(original_path).read_bytes() == corrupted_bytes


# ===========================================================================
# 17: retry after failure
# ===========================================================================
def test_retry_after_failure_succeeds_once_source_is_fixed(app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    media = _add_real_media(app_module, db_session, pair, f"{project.id}_0_retry.mp4")
    original_path = os.path.join(app_module.VIDEOS_DIR, media.video_filename)
    good_bytes = Path(original_path).read_bytes()
    Path(original_path).write_bytes(b"not a real video file")

    job, created = create_processing_job(
        "optimize_pair_media", project_id=project.id, pair_id=pair.id, pair_media_id=media.id,
    )
    assert created
    run_processing_job(job.id)
    db_session.expire_all()
    assert app_module.PairMedia.query.get(media.id).optimization_status == "failed"

    # "Fix" the source and retry - the SAME job id, exactly like a real RQ
    # retry re-invokes run_processing_job(job.id) rather than minting a new
    # job row (a fresh row for the same still-active PairMedia would just
    # dedupe back to this one anyway, per active_project_job()).
    Path(original_path).write_bytes(good_bytes)
    result2 = run_processing_job(job.id)
    db_session.expire_all()
    refreshed = app_module.PairMedia.query.get(media.id)
    assert result2["ok"] is True
    assert refreshed.optimization_status == "ready"
    assert refreshed.optimization_error is None


# ===========================================================================
# 18: ready rerun is idempotent (no duplicate derivative work)
# ===========================================================================
def test_rerunning_an_already_ready_media_is_a_noop(app_module, db_session, project_with_pair, monkeypatch):
    project, pair = project_with_pair
    media = _add_real_media(app_module, db_session, pair, f"{project.id}_0_idem.mp4")
    _run_job_for(app_module, db_session, media)
    ready = app_module.PairMedia.query.get(media.id)
    assert ready.optimization_status == "ready"
    first_optimized_at = ready.optimized_at
    derivative_path = os.path.join(app_module.VIDEOS_DIR, ready.optimized_video_filename)
    first_mtime = os.path.getmtime(derivative_path)

    calls = []
    monkeypatch.setattr(mo, "transcode_video", lambda *a, **k: calls.append(1))

    _run_job_for(app_module, db_session, media)
    still_ready = app_module.PairMedia.query.get(media.id)
    assert not calls, "transcode_video must not run again for an already-ready, still-present derivative"
    assert still_ready.optimized_at == first_optimized_at
    assert os.path.getmtime(derivative_path) == first_mtime


# ===========================================================================
# 19: missing derivative repairs safely
# ===========================================================================
def test_missing_derivative_file_triggers_safe_reprocessing(app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    media = _add_real_media(app_module, db_session, pair, f"{project.id}_0_repair.mp4")
    _run_job_for(app_module, db_session, media)
    ready = app_module.PairMedia.query.get(media.id)
    derivative_path = os.path.join(app_module.VIDEOS_DIR, ready.optimized_video_filename)
    assert os.path.exists(derivative_path)

    os.remove(derivative_path)  # simulate an out-of-band loss of the derivative file

    job2, result2 = _run_job_for(app_module, db_session, media)
    repaired = app_module.PairMedia.query.get(media.id)
    assert result2["ok"] is True
    assert repaired.optimization_status == "ready"
    assert os.path.exists(os.path.join(app_module.VIDEOS_DIR, repaired.optimized_video_filename))


# ===========================================================================
# 20: separate PairMedia rows get separate derivatives, never mixed
# ===========================================================================
def test_two_pair_media_rows_get_two_independent_derivatives(app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    media_a = _add_real_media(app_module, db_session, pair, f"{project.id}_0_x.mp4")
    media_b = _add_real_media(app_module, db_session, pair, f"{project.id}_0_y.mp4")

    _run_job_for(app_module, db_session, media_a)
    _run_job_for(app_module, db_session, media_b)

    ra = app_module.PairMedia.query.get(media_a.id)
    rb = app_module.PairMedia.query.get(media_b.id)
    assert ra.optimization_status == "ready" and rb.optimization_status == "ready"
    assert ra.optimized_video_filename != rb.optimized_video_filename
    assert os.path.exists(os.path.join(app_module.VIDEOS_DIR, ra.optimized_video_filename))
    assert os.path.exists(os.path.join(app_module.VIDEOS_DIR, rb.optimized_video_filename))


# ===========================================================================
# 21: derivative is never counted as a billable/uploaded MediaObject
# ===========================================================================
def test_derivative_creates_no_media_object_row(app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    media = _add_real_media(app_module, db_session, pair, f"{project.id}_0_quota.mp4")
    before = app_module.MediaObject.query.filter_by(project_id=project.id).count()

    _run_job_for(app_module, db_session, media)
    assert app_module.PairMedia.query.get(media.id).optimization_status == "ready"

    after = app_module.MediaObject.query.filter_by(project_id=project.id).count()
    assert after == before


# ===========================================================================
# 22: project deletion cleans up both original and derivative PairMedia files
# ===========================================================================
def test_project_deletion_removes_pair_media_original_and_derivative(app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    media = _add_real_media(app_module, db_session, pair, f"{project.id}_0_cleanup.mp4")
    _run_job_for(app_module, db_session, media)
    refreshed = app_module.PairMedia.query.get(media.id)
    original_path = os.path.join(app_module.VIDEOS_DIR, refreshed.video_filename)
    derivative_path = os.path.join(app_module.VIDEOS_DIR, refreshed.optimized_video_filename)
    assert os.path.exists(original_path)
    assert os.path.exists(derivative_path)

    app_module._delete_project_files_and_rows(project)

    assert not os.path.exists(original_path)
    assert not os.path.exists(derivative_path)
