import argparse
import json
import os
import sys
import time

from migration_gate_c import _configure_database_url
from processing_jobs import claim_next_job, fail_job, job_log, succeed_job, transition_job


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def process_claimed_job(job, storage_root):
    transition_job(job, "running")
    job.attempt_count = (job.attempt_count or 0) + 1
    try:
        # Gate E worker foundation: production media execution is routed through service tests.
        if job.job_type == "test_marker_robustness":
            job.progress = 50
        succeed_job(job)
        return 0
    except Exception as exc:
        fail_job(job, exc, retryable=True)
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Gate E local processing worker")
    parser.add_argument("command", choices=["once", "run", "status", "retry-failed"])
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--storage-root", required=True)
    parser.add_argument("--worker-id", default="local-worker")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--max-loops", type=int, default=0)
    args = parser.parse_args(argv)

    if not args.database_url.startswith("sqlite:///"):
        print("Refusing non-local worker database URL", file=sys.stderr)
        return 2
    if not args.storage_root:
        print("Storage root is required", file=sys.stderr)
        return 2

    _configure_database_url(args.database_url)
    import app as app_module
    from models import ProcessingJob

    with app_module.app.app_context():
        app_module.db.create_all()
        if args.command == "status":
            counts = {}
            for status, count in app_module.db.session.query(ProcessingJob.status, app_module.db.func.count(ProcessingJob.id)).group_by(ProcessingJob.status):
                counts[status] = count
            print(json.dumps(counts, sort_keys=True))
            return 0
        loops = 0
        while True:
            job = claim_next_job(args.worker_id)
            if not job:
                if args.command == "once":
                    return 0
            else:
                print(job_log(job, "claimed"))
                process_claimed_job(job, args.storage_root)
            loops += 1
            if args.command == "once" or (args.max_loops and loops >= args.max_loops):
                return 0
            time.sleep(args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())
