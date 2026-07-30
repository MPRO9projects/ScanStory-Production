# migration_script.py
from sqlalchemy import inspect, text
from app import app, db
from models import SubscriptionPlan, Project, User, Admin

with app.app_context():
    inspector = inspect(db.engine)
    if 'subscription_plans' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('subscription_plans')]
        if 'max_pairs_per_project' not in columns:
            print('Adding max_pairs_per_project column to subscription_plans...')
            try:
                with db.engine.connect() as connection:
                    connection.execute(text('ALTER TABLE subscription_plans ADD COLUMN max_pairs_per_project INTEGER'))
                    connection.commit()
                print('Column added successfully.')
            except Exception as e:
                print(f'Failed to add column: {e}')
        else:
            print('Column max_pairs_per_project already exists.')

    # Ensure projects table has per-owner index column
    if 'projects' in inspector.get_table_names():
        proj_columns = [col['name'] for col in inspector.get_columns('projects')]
        if 'user_project_index' not in proj_columns:
            print('Adding user_project_index column to projects...')
            try:
                with db.engine.connect() as connection:
                    connection.execute(text('ALTER TABLE projects ADD COLUMN user_project_index INTEGER'))
                    connection.commit()
                print('Column added successfully.')
            except Exception as e:
                print(f'Failed to add column to projects: {e}')
        else:
            print('Column user_project_index already exists on projects.')

    plans = SubscriptionPlan.query.all()
    updated = 0
    for plan in plans:
        changed = False
        if plan.total_project_limit == 999999:
            plan.total_project_limit = None
            changed = True
        if plan.total_scan_limit == 999999:
            plan.total_scan_limit = None
            changed = True
        if plan.max_pairs_per_project is None:
            # Keep missing pair limits as NULL so admin can explicitly configure them.
            changed = False
        if changed:
            updated += 1
            print(f"Updated: {plan.plan_name}")
    
    db.session.commit()
    print(f"\n✅ Updated {updated} plans. 999999 → NULL and preserved missing max_pairs_per_project values")

    # Backfill user_project_index for existing projects (per owner)
    try:
        backfilled = 0
        # Per-user projects
        users = User.query.all()
        for u in users:
            projects = Project.query.filter_by(owner_user_id=u.id).order_by(Project.created_at.asc()).all()
            idx = 1
            for p in projects:
                p.user_project_index = idx
                idx += 1
                backfilled += 1

        # Per-admin projects
        admins = Admin.query.all()
        for a in admins:
            projects = Project.query.filter_by(owner_admin_id=a.id).order_by(Project.created_at.asc()).all()
            idx = 1
            for p in projects:
                p.user_project_index = idx
                idx += 1
                backfilled += 1

        db.session.commit()
        print(f"\n✅ Backfilled {backfilled} project sequence indexes for owners (users/admins)")
    except Exception as e:
        print(f"Failed to backfill project indexes: {e}")