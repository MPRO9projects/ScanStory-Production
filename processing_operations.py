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
    standardization_ms = 0
    feature_start = time.perf_counter()
    try:
        if not project.owner_admin_id:
            standardization_start = time.perf_counter()
            app_module.standardize_uploaded_image(img_path, target_size=1200)
            standardization_ms = app_module._elapsed_ms(standardization_start)
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


def run_processing_job(job_id):
    import app as app_module

    with app_module.app.app_context():
        job = ProcessingJob.query.get(job_id)
        if not job:
            return {"ok": False, "reason": "missing_job"}
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
