# add_simple_admin.py
#
# Creates a single Admin row from operator-supplied credentials. No
# hard-coded email/password: this used to plant admin@gmail.com / admin123
# into the live database on every run, which was a standing backdoor
# credential committed to the repo.
#
# Required environment variables:
#   ADMIN_EMAIL              - new admin's email address
#   ADMIN_PASSWORD           - new admin's password (never printed/logged)
#   CONFIRM_ADMIN_CREATION=1 - explicit opt-in, refuses to run without it
#
# Example:
#   ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='...' CONFIRM_ADMIN_CREATION=1 \
#       python add_simple_admin.py
import os
import sys

from app import app, db
from models import Admin
from werkzeug.security import generate_password_hash


def main():
    email = os.environ.get("ADMIN_EMAIL", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")
    confirmed = os.environ.get("CONFIRM_ADMIN_CREATION", "").strip().lower() in ("1", "true", "yes")

    missing = []
    if not email:
        missing.append("ADMIN_EMAIL")
    if not password:
        missing.append("ADMIN_PASSWORD")
    if not confirmed:
        missing.append("CONFIRM_ADMIN_CREATION=1")

    if missing:
        print("Refusing to run - missing required value(s): " + ", ".join(missing), file=sys.stderr)
        sys.exit(1)

    with app.app_context():
        if Admin.query.filter_by(email=email).first():
            print(f"Refusing to run - an admin with email {email} already exists.", file=sys.stderr)
            sys.exit(1)

        admin = Admin(
            email=email,
            name="Admin User",
            password_hash=generate_password_hash(password),
            role="admin",
            is_active=True,
        )
        db.session.add(admin)
        db.session.commit()
        print(f"Admin created: {email}")


if __name__ == "__main__":
    main()
