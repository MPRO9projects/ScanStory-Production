import os
import time

from models import ProcessingJob, Project, ProjectPair, db
from processing_queue import mark_job_completed, mark_job_failed, mark_job_processing


def _dirs_for_project(app_module, project):
    if project.owner_admin_id:
        return app_module.ADMIN_IMAGES_DIR, app_module.ADMIN_FEATURES_DIR
    return app_module.IMAGES_DIR, app_module.FEATURES_DIR


def _process_pair(app_module, project, pair):
    image_dir, feature_dir = _dirs_for_project(app_module, project)
    img_path = os.path.join(image_dir, pair.image_filename)
    work_img_path = os.path.join(image_dir, f"{project.id}_{pair.pair_index}_work.jpg")
    npz_path = os.path.join(feature_dir, f"{project.id}_{pair.pair_index}.npz")
    if not os.path.exists(img_path):
        raise FileNotFoundError("source marker image missing")
    # The canonical target file (img_path) is standardized exactly once, synchronously,
    # by the upload/replace/add-target route that persists it and computes
    # ProjectPair.image_hash from that exact standardized output. standardize_uploaded_image()
    # is NOT byte-idempotent for real camera photos (a second re-encode pass measurably
    # changes the file), so it must never run again here - doing so silently drifted the
    # canonical file away from its own stored image_hash within seconds of every real
    # create/replace. Feature extraction needs no further standardization of the canonical
    # file: make_feature_working_jpeg below reads img_path directly and produces its own
    # independent, disposable working copy (resized/re-encoded to ORB_MAX_DIM) for ORB.
    standardization_ms = 0
    feature_start = time.perf_counter()
    try:
        app_module.make_feature_working_jpeg(img_path, work_img_path, max_dim=app_module.ORB_MAX_DIM, jpeg_quality=92)
        app_module.extract_features_multi(work_img_path, npz_path, max_dim=app_module.ORB_MAX_DIM)
        return {
            "image_standardization_duration_ms": standardization_ms,
            "feature_generation_duration_ms": app_module._elapsed_ms(feature_start),
        }
    finally:
        try:
            if os.path.exists(work_img_path):
                os.remove(work_img_path)
        except Exception:
            pass


def _notify_processing_terminal(app_module, project, ready):
    """Best-effort, one email per terminal transition. Admin-owned projects
    are skipped (an admin creates and sees the result synchronously in the
    console; this is a courtesy notification for a User who may have closed
    the tab). Never allowed to affect job/pair state - only ever called
    after that state is already final."""
    if not project.owner_user_id:
        return
    user = app_module.User.query.get(project.owner_user_id)
    if not user:
        return
    try:
        if ready:
            app_module.send_processing_ready_email(user, project)
        else:
            app_module.send_processing_failed_email(user, project)
    except Exception:
        app_module.app.logger.exception(
            "Failed to send processing-%s email for project %s",
            "ready" if ready else "failed", project.id,
        )


def run_processing_job(job_id):
    import app as app_module

    with app_module.app.app_context():
        job = ProcessingJob.query.get(job_id)
        if not job:
            return {"ok": False, "reason": "missing_job"}
        if job.job_type == "optimize_pair_media":
            # Self-contained (own terminal/claim checks below) rather than
            # sharing process_project_pairs' preamble, so this job type can
            # never affect that one's already-tested control flow.
            return _run_optimize_pair_media_job(app_module, job)
        if job.job_type != "process_project_pairs":
            mark_job_failed(job, "INVALID_JOB_TYPE", "Unknown processing job type.", retryable=False)
            return {"ok": False, "reason": "invalid_job_type"}
        if job.status in {"completed", "succeeded"}:
            return {"ok": True, "reason": "already_completed"}
        if job.status in {"failed", "cancelled", "superseded"}:
            return {"ok": False, "reason": "terminal"}
        if not mark_job_processing(job):
            db.session.expire_all()
            job = ProcessingJob.query.get(job_id)
            if job and job.status in {"completed", "succeeded"}:
                return {"ok": True, "reason": "already_completed"}
            return {"ok": False, "reason": "not_claimed"}

        processing_start = time.perf_counter()
        job = ProcessingJob.query.get(job_id)
        job.attempt_count = int(job.attempt_count or 0) + 1
        job.last_heartbeat_at = app_module.dt.utcnow()
        db.session.commit()
        queue_wait_ms = 0
        if job.queued_at and job.started_at:
            queue_wait_ms = max(0, (job.started_at - job.queued_at).total_seconds() * 1000)

        project = Project.query.get(job.project_id)
        if not project:
            mark_job_failed(job, "PROJECT_MISSING", "Project no longer exists.", retryable=False)
            app_module._log_processing_timing(
                "processing_job_run",
                job_id=job.id,
                project_id=job.project_id,
                job_type=job.job_type,
                queue_wait_duration_ms=queue_wait_ms,
                processing_duration_ms=app_module._elapsed_ms(processing_start),
                attempt_count=job.attempt_count,
                status="failed",
                safe_error_code="PROJECT_MISSING",
            )
            return {"ok": False, "reason": "project_missing"}

        pairs = ProjectPair.query.filter_by(project_id=project.id).order_by(ProjectPair.pair_index.asc()).all()
        if not pairs:
            mark_job_completed(job)
            app_module._log_processing_timing(
                "processing_job_run",
                job_id=job.id,
                project_id=project.id,
                job_type=job.job_type,
                queue_wait_duration_ms=queue_wait_ms,
                processing_duration_ms=app_module._elapsed_ms(processing_start),
                pair_count=0,
                attempt_count=job.attempt_count,
                status="completed",
            )
            return {"ok": True, "processed": 0}

        processed = 0
        failures = 0
        for pair in pairs:
            if pair.is_processed and pair.feature_extraction_status == "extracted" and not pair.processing_error:
                continue
            pair.processing_status = "processing"
            pair.feature_extraction_status = "extracting"
            pair.processing_error = None
            db.session.commit()
            start = time.time()
            pair_start = time.perf_counter()
            pair_timings = {}
            try:
                pair_timings = _process_pair(app_module, project, pair)
                pair.is_processed = True
                pair.processing_status = "completed"
                pair.feature_extraction_status = "extracted"
                pair.processing_error = None
                pair.feature_extraction_time = time.time() - start
                processed += 1
                app_module._log_processing_timing(
                    "processing_pair",
                    job_id=job.id,
                    project_id=project.id,
                    pair_id=pair.id,
                    pair_index=pair.pair_index,
                    pair_processing_duration_ms=app_module._elapsed_ms(pair_start),
                    feature_generation_duration_ms=pair_timings.get("feature_generation_duration_ms", 0),
                    image_standardization_duration_ms=pair_timings.get("image_standardization_duration_ms", 0),
                    status="completed",
                )
            except FileNotFoundError as exc:
                pair.is_processed = False
                pair.processing_status = "failed"
                pair.feature_extraction_status = "failed"
                pair.processing_error = "source marker image missing"
                failures += 1
                db.session.commit()
                mark_job_failed(job, "SOURCE_MISSING", exc, retryable=False)
                _notify_processing_terminal(app_module, project, ready=False)
                app_module._log_processing_timing(
                    "processing_pair",
                    job_id=job.id,
                    project_id=project.id,
                    pair_id=pair.id,
                    pair_index=pair.pair_index,
                    pair_processing_duration_ms=app_module._elapsed_ms(pair_start),
                    status="failed",
                    safe_error_code="SOURCE_MISSING",
                )
                app_module._log_processing_timing(
                    "processing_job_run",
                    job_id=job.id,
                    project_id=project.id,
                    job_type=job.job_type,
                    queue_wait_duration_ms=queue_wait_ms,
                    processing_duration_ms=app_module._elapsed_ms(processing_start),
                    pair_count=len(pairs),
                    attempt_count=job.attempt_count,
                    status="failed",
                    safe_error_code="SOURCE_MISSING",
                )
                return {"ok": False, "reason": "source_missing"}
            except Exception as exc:
                pair.is_processed = False
                pair.processing_status = "failed"
                pair.feature_extraction_status = "failed"
                pair.processing_error = "feature extraction failed"
                failures += 1
                app_module._log_processing_timing(
                    "processing_pair",
                    job_id=job.id,
                    project_id=project.id,
                    pair_id=pair.id,
                    pair_index=pair.pair_index,
                    pair_processing_duration_ms=app_module._elapsed_ms(pair_start),
                    feature_generation_duration_ms=pair_timings.get("feature_generation_duration_ms", 0),
                    image_standardization_duration_ms=pair_timings.get("image_standardization_duration_ms", 0),
                    status="failed",
                    safe_error_code="PAIR_PROCESSING_FAILED",
                )
            finally:
                db.session.commit()

        app_module.load_features.cache_clear()
        if failures:
            mark_job_failed(job, "PAIR_PROCESSING_FAILED", f"{failures} pair(s) failed.", retryable=True)
            # retryable=True means the worker may retry this same job - only
            # notify once it has actually reached the FINAL failed status
            # (attempts exhausted), never on an intermediate "retrying" one.
            if job.status == "failed":
                _notify_processing_terminal(app_module, project, ready=False)
            app_module._log_processing_timing(
                "processing_job_run",
                job_id=job.id,
                project_id=project.id,
                job_type=job.job_type,
                queue_wait_duration_ms=queue_wait_ms,
                processing_duration_ms=app_module._elapsed_ms(processing_start),
                pair_count=len(pairs),
                attempt_count=job.attempt_count,
                status="failed",
                safe_error_code="PAIR_PROCESSING_FAILED",
            )
            return {"ok": False, "processed": processed, "failed": failures}
        mark_job_completed(job)
        _notify_processing_terminal(app_module, project, ready=True)
        app_module._log_processing_timing(
            "processing_job_run",
            job_id=job.id,
            project_id=project.id,
            job_type=job.job_type,
            queue_wait_duration_ms=queue_wait_ms,
            processing_duration_ms=app_module._elapsed_ms(processing_start),
            pair_count=len(pairs),
            attempt_count=job.attempt_count,
            status="completed",
        )
        return {"ok": True, "processed": processed}


def _cleanup_temp_file(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _run_optimize_pair_media_job(app_module, job):
    """Fast Video Phase 1: transcode one PairMedia's original video into an
    optimized MP4 derivative. video_filename (the original) is never opened
    for writing anywhere in this function - only read as ffmpeg's input -
    so a failure at any step leaves it exactly as it was."""
    import media_optimization as mo
    from models import PairMedia

    if job.status in {"completed", "succeeded"}:
        return {"ok": True, "reason": "already_completed"}
    if job.status in {"failed", "cancelled", "superseded"}:
        return {"ok": False, "reason": "terminal"}

    media = PairMedia.query.get(job.pair_media_id)
    if not media:
        mark_job_failed(job, "PAIR_MEDIA_MISSING", "PairMedia no longer exists.", retryable=False)
        return {"ok": False, "reason": "pair_media_missing"}
    pair = ProjectPair.query.get(media.pair_id)
    project = Project.query.get(pair.project_id) if pair else None
    if not pair or not project:
        mark_job_failed(job, "PAIR_MISSING", "Pair or project no longer exists.", retryable=False)
        return {"ok": False, "reason": "pair_missing"}

    videos_dir = app_module.project_media_dirs(project)[1]
    original_path = os.path.join(videos_dir, media.video_filename)
    if not os.path.exists(original_path):
        media.optimization_status = "failed"
        media.optimization_error = "Original video file is missing."
        db.session.commit()
        mark_job_failed(job, "SOURCE_MISSING", "Original video file is missing.", retryable=False)
        return {"ok": False, "reason": "source_missing"}

    # Idempotency: genuinely ready and the file is still there -> nothing to do.
    if media.optimization_status == "ready" and media.optimized_video_filename:
        if os.path.exists(os.path.join(videos_dir, media.optimized_video_filename)):
            mark_job_completed(job)
            return {"ok": True, "reason": "already_ready"}
        # status says ready but the file is missing - fall through and repair.

    if not mark_job_processing(job):
        db.session.expire_all()
        job = ProcessingJob.query.get(job.id)
        if job and job.status in {"completed", "succeeded"}:
            return {"ok": True, "reason": "already_completed"}
        return {"ok": False, "reason": "not_claimed"}

    job.attempt_count = int(job.attempt_count or 0) + 1
    job.last_heartbeat_at = app_module.dt.utcnow()
    media.optimization_status = "processing"
    media.optimization_error = None
    db.session.commit()

    ffmpeg_bin = mo.resolve_ffmpeg_binary()
    ffprobe_bin = mo.resolve_ffprobe_binary()
    if not ffmpeg_bin or not ffprobe_bin:
        message = "Video optimization tool is not available."
        media.optimization_status = "failed"
        media.optimization_error = message
        db.session.commit()
        mark_job_failed(job, "BINARY_UNAVAILABLE", message, retryable=True)
        return {"ok": False, "reason": "binary_unavailable"}

    final_name = mo.derivative_filename(project.id, pair.id, media.id)
    final_path = os.path.join(videos_dir, final_name)
    # The job-id/tmp marker is a PREFIX, not a suffix - ffmpeg infers its
    # output muxer from the file extension when none is given via -f, so the
    # temp name must still end in .mp4 or it fails with a generic
    # "Error opening output files: Invalid argument".
    temp_path = os.path.join(videos_dir, f".tmp-{job.id}-{final_name}")

    try:
        probe = mo.probe_video(ffprobe_bin, original_path)
        mo.transcode_video(ffmpeg_bin, original_path, temp_path, has_audio=probe["has_audio"])
    except mo.TranscodeError as exc:
        _cleanup_temp_file(temp_path)
        media.optimization_status = "failed"
        media.optimization_error = str(exc)
        db.session.commit()
        mark_job_failed(job, "TRANSCODE_FAILED", str(exc), retryable=True)
        return {"ok": False, "reason": "transcode_failed"}

    original_size = media.video_size or os.path.getsize(original_path)
    optimized_size = os.path.getsize(temp_path)

    if not mo.should_retain_derivative(original_size, optimized_size):
        _cleanup_temp_file(temp_path)
        _cleanup_temp_file(final_path)  # a stale derivative from an earlier attempt, if any
        media.optimization_status = "failed"
        media.optimization_error = (
            f"Optimized derivative ({optimized_size}B) did not shrink the original "
            f"({original_size}B) enough to keep; original remains the playback source."
        )
        media.optimized_video_filename = None
        media.optimized_video_size = None
        db.session.commit()
        # The JOB ran to completion without error - PairMedia's own status
        # records the business outcome ("not worth keeping"), which is
        # distinct from "the job crashed".
        mark_job_completed(job)
        return {"ok": True, "reason": "not_worth_retaining"}

    os.replace(temp_path, final_path)  # atomic - never written directly to the final name
    media.optimized_video_filename = final_name
    media.optimization_status = "ready"
    media.optimization_error = None
    media.optimized_video_size = optimized_size
    media.optimized_at = app_module.dt.utcnow()
    db.session.commit()
    mark_job_completed(job)
    return {"ok": True, "reason": "optimized"}
