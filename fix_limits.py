# fix_limits.py
from app import app, db
from models import SubscriptionPlan

with app.app_context():
    plans = SubscriptionPlan.query.all()
    updated = 0
    
    for plan in plans:
        changed = False
        
        # Check if project limit is 999999 (unlimited)
        if plan.total_project_limit == 999999:
            plan.total_project_limit = None
            changed = True
            print(f"✅ {plan.plan_name}: Projects 999999 → NULL (Unlimited)")
        
        # Check if scan limit is 999999 (unlimited)
        if plan.total_scan_limit == 999999:
            plan.total_scan_limit = None
            changed = True
            print(f"✅ {plan.plan_name}: Scans 999999 → NULL (Unlimited)")
        
        # Also check if there are any None values that should be shown as Unlimited
        if changed:
            updated += 1
    
    db.session.commit()
    print(f"\n✅ Updated {updated} plans. All unlimited plans now use NULL instead of 999999")
    
    # Verify the changes
    print("\n📊 Updated Plans:")
    for plan in plans:
        projects = "∞ Unlimited" if plan.total_project_limit is None else plan.total_project_limit
        scans = "∞ Unlimited" if plan.total_scan_limit is None else plan.total_scan_limit
        print(f"  {plan.plan_name}: Projects = {projects}, Scans = {scans}")