"""Throwaway isolated app instance for the throttled upload certification.

Mirrors tests/conftest.py's isolated_app fixture: SCANSTORY_TESTING=1, a
temp SQLite DB, temp data dirs, no external network, plus one verified
creator seeded so the certification driver can log in. Nothing here touches
a real database or a real credential, and nothing here is imported by the
application.

Usage (dev only):
    python scripts/dev/low_bandwidth_upload_server.py --port 5099
"""
import argparse
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

CREATOR_EMAIL = "lowbandwidth@example.com"
CREATOR_PASSWORD = "password123"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5099)
    parser.add_argument("--chunk-max-bytes", type=int, default=1024 * 1024)
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="scanstory_lowbw_"))
    os.environ["SCANSTORY_TESTING"] = "1"
    os.environ["TEST_DATABASE_URL"] = f"sqlite:///{(workdir / 'certification.db').as_posix()}"
    os.environ["SCANSTORY_DATA_DIR"] = str(workdir / "data")
    os.environ["SCANSTORY_ADMIN_DATA_DIR"] = str(workdir / "data_admin")
    os.environ["SCANSTORY_STATIC_UPLOADS_DIR"] = str(workdir / "static_uploads")
    os.environ["FLASK_SECRET_KEY"] = "low-bandwidth-certification-secret"
    os.environ["SCANSTORY_RESUMABLE_CHUNK_MAX_BYTES"] = str(args.chunk_max_bytes)
    os.environ.pop("DATABASE_URL", None)

    import app as app_module  # noqa: E402  (env must be set first)
    from werkzeug.security import generate_password_hash  # noqa: E402

    assert app_module.app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///"), \
        "refusing to run the certification harness against a non-sqlite database"

    with app_module.app.app_context():
        db = app_module.db
        plan = app_module.SubscriptionPlan.query.filter_by(is_trial_plan=True).first()
        existing = app_module.User.query.filter_by(email=CREATOR_EMAIL).first()
        if not existing:
            user = app_module.User(
                email=CREATOR_EMAIL,
                first_name="Low",
                last_name="Bandwidth",
                password_hash=generate_password_hash(CREATOR_PASSWORD),
                is_verified=True,
                subscription_id=plan.id if plan else None,
                subscription_status="trial",
                subscription_taken_at=datetime.utcnow(),
                subscribed_project_limit=(plan.total_project_limit if plan else 100),
                subscribed_scan_limit=(plan.total_scan_limit if plan else 1000),
                projects_used=0,
                scans_used=0,
            )
            db.session.add(user)
            db.session.commit()

    print(f"READY port={args.port} workdir={workdir}", flush=True)
    # threaded so a stalled throttled upload cannot block the status reads
    # the driver interleaves with it.
    app_module.app.run(host="127.0.0.1", port=args.port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
