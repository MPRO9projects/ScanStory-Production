import argparse
import json
import os
import sys
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash


SIZES = {
    "small": {"users": 10, "projects": 20, "pairs": 50},
    "medium": {"users": 30, "projects": 90, "pairs": 250},
    "large": {"users": 60, "projects": 180, "pairs": 500},
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def configure(database_url):
    os.environ["SCANSTORY_TESTING"] = "1"
    os.environ["TEST_DATABASE_URL"] = database_url
    os.environ["FLASK_SECRET_KEY"] = "gate-d-rehearsal-secret"
    os.environ["RAZORPAY_KEY_ID"] = ""
    os.environ["RAZORPAY_KEY_SECRET"] = ""


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build a masked synthetic Gate D rehearsal database")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--size", choices=sorted(SIZES), default="small")
    args = parser.parse_args(argv)
    configure(args.database_url)

    import app as app_module
    from models import Admin, AdminActivity, PaymentOrder, Project, ProjectPair, ScanLog, SubscriptionPlan, TrialDetails, User, UserLoginActivity, db

    cfg = SIZES[args.size]
    with app_module.app.app_context():
        db.drop_all()
        db.create_all()

        plan = SubscriptionPlan.query.filter_by(is_trial_plan=True).first()
        if not plan:
            plan = SubscriptionPlan(
                plan_name="Masked Trial",
                plan_description="Synthetic trial plan",
                plan_amount=0,
                is_trial_plan=True,
                total_project_limit=100,
                total_scan_limit=1000,
            )
            db.session.add(plan)
            db.session.flush()
        paid_plan = SubscriptionPlan.query.filter_by(is_trial_plan=False).first()
        if not paid_plan:
            paid_plan = SubscriptionPlan(
                plan_name="Masked Paid",
                plan_description="Synthetic paid plan",
                plan_amount=100,
                offer_price=80,
                is_trial_plan=False,
                total_project_limit=100,
                total_scan_limit=1000,
            )
            db.session.add(paid_plan)
            db.session.flush()
        admin = Admin.query.filter_by(email="admin@scanstory.com").first()
        if not admin:
            admin = Admin(
                email="admin@scanstory.test",
                name="Masked Admin",
                password_hash=generate_password_hash("Admin@123"),
                role="superadmin",
                is_active=True,
            )
            db.session.add(admin)
            db.session.flush()

        users = []
        for idx in range(cfg["users"]):
            is_expired = idx % 7 == 0
            user = User(
                email=f"user{idx}@example.test",
                first_name=f"User{idx}",
                last_name="Masked",
                phone="0000000000",
                password_hash=generate_password_hash("password123"),
                is_verified=True,
                is_blocked=(idx % 11 == 0),
                subscription_id=paid_plan.id if idx % 5 == 0 else plan.id,
                subscription_status="expired" if is_expired else ("active" if idx % 5 == 0 else "trial"),
                subscribed_project_limit=100,
                subscribed_scan_limit=1000,
            )
            db.session.add(user)
            db.session.flush()
            db.session.add(
                TrialDetails(
                    user_id=user.id,
                    trial_start=datetime.utcnow() - timedelta(days=idx),
                    trial_end=datetime.utcnow() + timedelta(days=(-1 if is_expired else 7)),
                )
            )
            db.session.add(UserLoginActivity(user_id=user.id, ip_address="127.0.0.1", is_successful=True))
            users.append(user)

        projects = []
        for idx in range(cfg["projects"]):
            if idx % 13 == 0:
                project = Project(name=f"Admin Project {idx}", owner_admin_id=admin.id)
            elif idx % 17 == 0:
                project = Project(name=f"Unknown Owner Project {idx}")
            else:
                owner = users[idx % len(users)]
                project = Project(name=f"Project {idx}", owner_user_id=owner.id, user_project_index=idx + 1)
            db.session.add(project)
            db.session.flush()
            project.scanner_url = f"/scanner/{project.id}"
            project.qr_code_filename = f"project_{project.id}_main.png"
            project.qr_code_path = f"/qr/project_{project.id}_main.png"
            projects.append(project)

        for idx in range(cfg["pairs"]):
            project = projects[idx % len(projects)]
            pair_index = idx // len(projects)
            image_filename = "" if idx % 19 == 0 else f"{project.id}_{pair_index}.jpg"
            video_filename = "" if idx % 23 == 0 else f"{project.id}_{pair_index}.mp4"
            pair = ProjectPair(
                project_id=project.id,
                pair_index=pair_index,
                image_filename=image_filename or "missing.jpg",
                video_filename=video_filename or "missing.mp4",
                image_path=f"/image/{project.id}/{pair_index}" if image_filename else "",
                is_processed=idx % 3 != 0,
                processing_status="completed" if idx % 3 != 0 else "uploaded",
                feature_extraction_status="extracted" if idx % 4 != 0 else "pending",
            )
            db.session.add(pair)
            db.session.flush()
            db.session.add(
                ScanLog(
                    project_id=project.id,
                    pair_id=pair.id,
                    user_id=users[idx % len(users)].id,
                    scan_session_id=f"session-{idx}",
                    is_successful=idx % 2 == 0,
                    counted=True,
                )
            )

        for idx, user in enumerate(users[: max(1, len(users) // 5)]):
            db.session.add(
                PaymentOrder(
                    order_id=f"ORD-MASKED-{idx}",
                    razorpay_order_id=f"order_masked_{idx}",
                    user_id=user.id,
                    plan_id=paid_plan.id,
                    amount=paid_plan.plan_amount,
                    total_amount=paid_plan.effective_price,
                    currency=paid_plan.currency,
                    status="success",
                )
            )
        db.session.add(AdminActivity(admin_id=admin.id, activity_type="rehearsal", description="masked synthetic data"))
        db.session.commit()

        payload = {
            "size": args.size,
            "users": User.query.count(),
            "admins": Admin.query.count(),
            "projects": Project.query.count(),
            "project_pairs": ProjectPair.query.count(),
            "scan_logs": ScanLog.query.count(),
            "payments": PaymentOrder.query.count(),
            "plans": SubscriptionPlan.query.count(),
            "trials": TrialDetails.query.count(),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
