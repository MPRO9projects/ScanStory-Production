import os
import re
import uuid
from datetime import timedelta

from public_keys import generate_unique_public_key
from models import Admin, ProcessingJob, Project, ProjectPair, User, db, get_utc_now


QUEUE_JOB_TYPES = {"process_project_pairs"}
ACTIVE_STATUSES = {"queued", "processing", "retrying", "ready", "claimed", "running", "retry_scheduled"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "superseded", "succeeded", "failed_terminal"}
SAFE_ERROR_LIMIT = 500
QUEUE_MODES = {"fake", "inline", "rq"}


class QueueUnavailable(RuntimeError):
    pass


class InvalidJobType(ValueError):
    pass


def queue_required():
    env = (os.environ.get("FLASK_ENV") or os.environ.get("APP_ENV") or "").strip().lower()
    return (
        os.environ.get("SCANSTORY_QUEUE_REQUIRED") == "1"
        or os.environ.get("SCANSTORY_PRODUCTION") == "1"
        or env == "production"
    )


def _queue_mode():
    explicit = (os.environ.get("SCANSTORY_QUEUE_MODE") or "").strip().lower()
    if explicit:
        return explicit
    if queue_required():
        return "rq"
    if os.environ.get("SCANSTORY_TESTING") == "1":
        return "fake"
    if os.environ.get("REDIS_URL"):
        return "rq"
    return "fake"


def queue_mode():
    mode = _queue_mode()
    if mode not in QUEUE_MODES:
        raise QueueUnavailable(f"unsupported queue mode: {mode}")
    return mode


def rq_timeout_seconds():
    try:
        return max(1, int(os.environ.get("RQ_DEFAULT_TIMEOUT", "600")))
    except (TypeError, ValueError):
        raise QueueUnavailable("RQ_DEFAULT_TIMEOUT must be a positive integer")


def queue_name():
    return (os.environ.get("RQ_QUEUE_NAME") or "scanstory-processing").strip() or "scanstory-processing"


def queue_config_summary():
    mode = queue_mode()
    redis_configured = bool(os.environ.get("REDIS_URL"))
    if mode == "rq" and not redis_configured:
        raise QueueUnavailable("REDIS_URL is required when SCANSTORY_QUEUE_MODE=rq")
    return {
        "mode": mode,
        "redis_configured": redis_configured,
        "queue_name": queue_name(),
        "timeout_seconds": rq_timeout_seconds(),
    }


def queue_available():
    if queue_required() and not os.environ.get("REDIS_URL"):
        return False
    mode = queue_mode()
    if mode in {"fake", "inline"}:
        # Fail closed in production (P0-6). 'fake' creates the ProcessingJob row
        # and never runs anything; reporting that as an available queue is what
        # let /ready stay green over a dead pipeline.
        return not queue_required()
    return bool(os.environ.get("REDIS_URL"))


def safe_error_summary(error):
    text = str(error or "processing failed").replace("\x00", " ")
    text = re.sub(r"[A-Za-z]:[\\/][^\s]+", "[path]", text)
    text = re.sub(r"/(?:[^/\s]+/){2,}[^\s]+", "[path]", text)
    text = re.sub(r"(?i)(secret|token|password|signature)=\S+", r"\1=[redacted]", text)
    return text[:SAFE_ERROR_LIMIT]


def _owner_fields(project):
    return {
        "owner_user_id": project.owner_user_id,
        "owner_admin_id": project.owner_admin_id,
    }


def active_project_job(project_id, job_type="process_project_pairs"):
    return ProcessingJob.query.filter(
        ProcessingJob.project_id == project_id,
        ProcessingJob.job_type == job_type,
        ProcessingJob.status.in_(ACTIVE_STATUSES),
    ).order_by(ProcessingJob.created_at.desc()).first()


def _project_job_idempotency_key(job_type, project_id=None, pair_id=None, attempt_scope="initial"):
    base = f"{job_type}:project:{project_id or '-'}:pair:{pair_id or '-'}"
    if attempt_scope == "reprocess":
        return f"{base}:attempt:{uuid.uuid4().hex}"
    return base


def _lock_project_for_job(project_id):
    if not project_id:
        return None
    return Project.query.filter(Project.id == project_id).with_for_update().first()


def create_processing_job(
    job_type,
    project_id=None,
    pair_id=None,
    owner_user_id=None,
    owner_admin_id=None,
    max_attempts=None,
    attempt_scope="initial",
):
    if job_type not in QUEUE_JOB_TYPES:
        raise InvalidJobType("unknown processing job type")
    if project_id:
        _lock_project_for_job(project_id)
        existing = active_project_job(project_id, job_type)
        if existing:
            return existing, False
    job = ProcessingJob(
        public_key=generate_unique_public_key(db.session, ProcessingJob, "job"),
        workspace_id=None,
        job_type=job_type,
        status="queued",
        project_id=project_id,
        pair_id=pair_id,
        owner_user_id=owner_user_id,
        owner_admin_id=owner_admin_id,
        idempotency_key=_project_job_idempotency_key(job_type, project_id, pair_id, attempt_scope),
        max_attempts=max_attempts or int(os.environ.get("RQ_MAX_RETRIES", "3")),
        queued_at=get_utc_now(),
        available_at=get_utc_now(),
    )
    db.session.add(job)
    db.session.commit()
    return job, True


def _enqueue_transport(job):
    mode = queue_mode()
    if mode == "inline":
        from processing_operations import run_processing_job
        run_processing_job(job.id)
        return f"inline-{job.id}"
    if mode == "fake":
        return f"fake-{job.id}"
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        raise QueueUnavailable("REDIS_URL is required when SCANSTORY_QUEUE_MODE=rq")
    try:
        from redis import Redis
        from rq import Queue, Retry
    except Exception as exc:
        raise QueueUnavailable("queue dependency unavailable") from exc
    try:
        conn = Redis.from_url(redis_url)
        conn.ping()
        queue = Queue(queue_name(), connection=conn)
        timeout = rq_timeout_seconds()
        retry_count = max(0, int(job.max_attempts or 1) - 1)
        retry = Retry(max=retry_count, interval=[30, 120, 300, 900]) if retry_count else None
        queued = queue.enqueue(
            "processing_operations.run_processing_job",
            job.id,
            job_timeout=timeout,
            retry=retry,
        )
        return queued.id
    except Exception as exc:
        raise QueueUnavailable("queue unavailable") from exc


def enqueue_processing_job(
    job_type,
    project_id=None,
    pair_id=None,
    owner_user_id=None,
    owner_admin_id=None,
    attempt_scope="initial",
):
    job, created = create_processing_job(
        job_type,
        project_id=project_id,
        pair_id=pair_id,
        owner_user_id=owner_user_id,
        owner_admin_id=owner_admin_id,
        attempt_scope=attempt_scope,
    )
    if not created:
        return job, False
    if queue_required() and not queue_available():
        job.status = "failed"
        job.failed_at = get_utc_now()
        job.completed_at = get_utc_now()
        job.safe_error_code = "QUEUE_UNAVAILABLE"
        job.safe_error_summary = "Processing queue is required but unavailable."
        job.error_code = job.safe_error_code
        job.error_message = job.safe_error_summary
        db.session.commit()
        raise QueueUnavailable("queue required but unavailable")
    try:
        queue_id = _enqueue_transport(job)
        job.queue_job_id = queue_id
        job.safe_error_code = None
        job.safe_error_summary = None
        db.session.commit()
        return job, created
    except Exception as exc:
        job.status = "failed"
        job.failed_at = get_utc_now()
        job.completed_at = get_utc_now()
        job.safe_error_code = "QUEUE_UNAVAILABLE"
        job.safe_error_summary = "Processing queue is unavailable."
        job.error_code = job.safe_error_code
        job.error_message = safe_error_summary(exc)
        db.session.commit()
        raise QueueUnavailable("queue unavailable") from exc


def enqueue_project_pair_processing(project_id, attempt_scope="initial"):
    project = Project.query.get(project_id)
    if not project:
        raise ValueError("project not found")
    return enqueue_processing_job("process_project_pairs", project_id=project.id, attempt_scope=attempt_scope, **_owner_fields(project))


def mark_job_processing(job):
    updated = ProcessingJob.query.filter(
        ProcessingJob.id == job.id,
        ProcessingJob.status.in_(["queued", "retrying", "ready", "claimed", "retry_scheduled"]),
    ).update(
        {
            ProcessingJob.status: "processing",
            ProcessingJob.started_at: job.started_at or get_utc_now(),
            ProcessingJob.last_heartbeat_at: get_utc_now(),
        },
        synchronize_session=False,
    )
    db.session.commit()
    return updated == 1


def mark_job_completed(job):
    job.status = "completed"
    job.progress = 100
    job.completed_at = get_utc_now()
    job.last_heartbeat_at = get_utc_now()
    job.safe_error_code = None
    job.safe_error_summary = None
    db.session.commit()
    return job


def mark_job_failed(job, code, summary, retryable=True):
    job.attempt_count = int(job.attempt_count or 0)
    retryable = bool(retryable and job.attempt_count < int(job.max_attempts or 1))
    job.safe_error_code = code
    job.safe_error_summary = safe_error_summary(summary)
    job.error_code = code
    job.error_message = job.safe_error_summary
    job.failed_at = get_utc_now()
    job.last_heartbeat_at = get_utc_now()
    if retryable:
        job.status = "retrying"
        delay = min(900, 30 * (2 ** max(0, job.attempt_count - 1)))
        job.available_at = get_utc_now() + timedelta(seconds=delay)
    else:
        job.status = "failed"
        job.completed_at = get_utc_now()
    db.session.commit()
    return job


def retry_failed_job(job_id):
    job = ProcessingJob.query.get(job_id)
    if not job or job.status != "failed":
        return None, False
    if int(job.attempt_count or 0) >= int(job.max_attempts or 1):
        return job, False
    job.status = "retrying"
    job.completed_at = None
    job.available_at = get_utc_now()
    job.safe_error_code = None
    job.safe_error_summary = None
    db.session.commit()
    job.queue_job_id = _enqueue_transport(job)
    db.session.commit()
    return job, True


def processing_job_status_payload(job):
    retry_eligible = job.status == "failed" and int(job.attempt_count or 0) < int(job.max_attempts or 1)
    return {
        "id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "project_id": job.project_id,
        "pair_id": job.pair_id,
        "queued_at": job.queued_at.isoformat() if job.queued_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "failed_at": job.failed_at.isoformat() if job.failed_at else None,
        "safe_error_code": job.safe_error_code,
        "safe_error_summary": job.safe_error_summary,
        "retry_eligible": retry_eligible,
    }


def _worker_stale_after_seconds():
    # A knob, not a constant: RQ's own worker TTL is configurable and a slow
    # host legitimately heartbeats late. Default is generous - roughly RQ's
    # 420s default worker_ttl - so a busy-but-alive worker is never called dead.
    try:
        return max(1, int(os.environ.get("RQ_WORKER_STALE_AFTER_SECONDS", "420")))
    except (TypeError, ValueError):
        return 420


def _rq_workers_for_queue():
    """Live RQ worker records registered against OUR queue.

    Separate function so readiness can be tested without a Redis server. RQ's
    own registry already drops a worker whose heartbeat key expired; the
    heartbeat age check in queue_worker_state() is a second, explicit guard so a
    registry that has not been reaped yet cannot report a dead worker as usable.
    """
    from redis import Redis
    from rq import Queue, Worker

    connection = Redis.from_url(os.environ["REDIS_URL"])
    return Worker.all(queue=Queue(queue_name(), connection=connection))


def queue_worker_state():
    """("ok" | "unavailable" | "not_applicable", usable_worker_count).

    'not_applicable' is the honest answer for queue modes that have no worker
    process by definition (fake/inline) - it is NOT a failure, and it is exactly
    the intentionally-supported non-rq deployment mode.

    Deliberately returns a COUNT and nothing else: worker names are hostname-pid
    strings and current jobs carry payloads, neither of which belongs in a
    public readiness response.
    """
    try:
        mode = queue_mode()
    except QueueUnavailable:
        return "unavailable", 0
    if mode != "rq":
        return "not_applicable", 0
    if not os.environ.get("REDIS_URL"):
        return "unavailable", 0
    try:
        workers = _rq_workers_for_queue()
    except Exception:
        return "unavailable", 0
    cutoff = get_utc_now() - timedelta(seconds=_worker_stale_after_seconds())
    usable = 0
    for worker in workers:
        if getattr(worker, "death_date", None):
            continue
        heartbeat = getattr(worker, "last_heartbeat", None)
        if heartbeat is not None and heartbeat < cutoff:
            continue
        usable += 1
    return ("ok" if usable else "unavailable"), usable


def redis_ready_check():
    if queue_required() and not os.environ.get("REDIS_URL"):
        return False
    try:
        mode = queue_mode()
    except QueueUnavailable:
        return False
    if mode in {"fake", "inline"}:
        return True
    if not os.environ.get("REDIS_URL"):
        return False
    try:
        from redis import Redis
        Redis.from_url(os.environ["REDIS_URL"]).ping()
        return True
    except Exception:
        return False
