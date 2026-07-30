# add_simple_admin.py
from app import app, db
from models import Admin
from werkzeug.security import generate_password_hash

with app.app_context():
    admin = Admin(
        email="admin@gmail.com",
        name="Admin User",
        password_hash=generate_password_hash("admin123"),
        role="admin",
        is_active=True
    )
    db.session.add(admin)
    db.session.commit()
    print("✅ Admin created: admin@gmail.com / admin@12345")