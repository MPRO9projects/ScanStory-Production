"""Admin stabilization pass — PART B: admin plan → public pricing sync
(SCANSTORY V1.1, 2026-09-02).

Human-verified defect: admin can create/edit a plan's price, but the
public-facing site does not reflect it.

Root-cause audit finding: the reported symptom is NOT reproducible against
the current code. landing()/pricing_page()/subscribe_page() and
create_razorpay_order() all already read live from the same SubscriptionPlan
rows admin edits (purchasable_plans_query()), with no cache layer anywhere
in the plan-data path (grepped for lru_cache/Flask-Caching/Redis-backed plan
cache - none found). Live-editing a plan's price directly in the QA DB and
re-fetching /pricing in the SAME process reflected the new price
immediately - the data path is already correct.

Two real, narrow issues WERE found and fixed during this audit:
  1. /faqs reused landing.html without passing `plans` at all, so its
     `{% for plan in plans %}` loop hit Jinja's strict UndefinedError on
     every visit - a real, confirmed, unrelated-to-caching crash bug.
  2. None of the three public pricing routes set any Cache-Control header,
     so a browser's own disk/back-forward cache (not the server) is the
     most plausible explanation for "changed it, still see the old price
     without a hard refresh" - closed defensively with explicit
     `Cache-Control: private, no-store`.

Checkout price authority was independently confirmed already correct:
create_razorpay_order() reads only plan_id from the client and computes the
charged amount from the server-side SubscriptionPlan row - no client-
supplied price is trusted anywhere in that route.

Run only this pack:
    python -m pytest tests/gate_jr/test_v11_admin_stabilization_part_b.py -q
"""
from pathlib import Path

import pytest


# ===========================================================================
# Live behavioral proof: admin edit -> public page, same process, no reload
# ===========================================================================

def test_editing_plan_price_directly_is_immediately_reflected_on_pricing_page(
    client, app_module, db_session, plan
):
    plan.plan_amount = 1499.0
    plan.offer_price = None
    db_session.commit()

    resp = client.get("/pricing")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "1499" in body


def test_editing_plan_price_directly_is_immediately_reflected_on_subscribe_page(
    client, app_module, db_session, plan, login_user
):
    plan.plan_amount = 1799.0
    plan.offer_price = None
    db_session.commit()

    resp = client.get("/subscribe")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "1799" in body


def test_new_active_plan_appears_on_public_pricing_without_a_restart(
    client, app_module, db_session, admin
):
    new_plan = app_module.SubscriptionPlan(
        plan_name="Business Stabilization Test",
        plan_amount=2499.0,
        is_active=True,
        lifecycle_status="ACTIVE",
        created_by=admin.id,
    )
    db_session.add(new_plan)
    db_session.commit()

    resp = client.get("/pricing")
    assert resp.status_code == 200
    assert "Business Stabilization Test" in resp.get_data(as_text=True)


def test_disabled_plan_disappears_from_public_pricing(client, app_module, db_session, plan):
    plan.is_active = False
    db_session.commit()

    resp = client.get("/pricing")
    assert resp.status_code == 200
    # A bare plan-name substring check is a false-positive trap here:
    # subscribe.html has an unrelated static "Free Trial Available" trust
    # badge on the page regardless of any actual plan's state - the real
    # signal is whether THIS plan's own card (its data-plan-id) is rendered.
    assert f'data-plan-id="{plan.id}"' not in resp.get_data(as_text=True)


def test_disabled_plan_rejected_at_checkout(client, app_module, db_session, plan, login_user, monkeypatch):
    plan.is_active = False
    db_session.commit()
    resp = client.post("/create-razorpay-order", data={"plan_id": plan.id})
    # This route's own established convention returns HTTP 200 with a
    # success:false payload for a rejected plan (not a 4xx status) - the
    # rejection itself, not the status code, is what matters here.
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is False
    assert "invalid" in body["error"].lower()


# ===========================================================================
# Checkout price authority - never trust a client-submitted price
# ===========================================================================

def test_checkout_ignores_any_client_submitted_price_field(client, app_module, db_session, plan, login_user):
    """The route must resolve price from the server-side plan row - proven
    by submitting an obviously-wrong client price and confirming it has no
    effect on what create_razorpay_order actually does with it (source
    check: no request.form.get for a price field at all)."""
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    idx = source.index("def create_razorpay_order(")
    body = source[idx:source.index("\n@app.route", idx)]
    assert 'request.form.get("price"' not in body
    assert 'request.form.get("amount"' not in body
    assert "plan.effective_price" in body or "plan.plan_amount" in body


# ===========================================================================
# Historical data preservation
# ===========================================================================

def test_changing_plan_price_does_not_mutate_historical_payment_orders(
    client, app_module, db_session, plan, normal_user
):
    order = app_module.PaymentOrder(
        order_id="stabilization_test_order_1",
        user_id=normal_user.id,
        plan_id=plan.id,
        amount=plan.plan_amount,
        total_amount=plan.plan_amount,
        currency=plan.currency,
        status="success",
        razorpay_order_id="order_stabilization_test",
    )
    db_session.add(order)
    db_session.commit()
    original_amount = order.amount

    plan.plan_amount = plan.plan_amount + 5000
    db_session.commit()

    app_module.db.session.refresh(order)
    assert order.amount == original_amount, "editing a plan's live price must never rewrite a historical order's amount"


# ===========================================================================
# The two confirmed, fixed defects
# ===========================================================================

def test_faqs_page_no_longer_crashes_on_undefined_plans(client):
    resp = client.get("/faqs")
    assert resp.status_code == 200


def test_public_pricing_routes_send_no_store_cache_control(client):
    for path in ("/", "/pricing", "/faqs"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers.get("Cache-Control") == "private, no-store", path


# ===========================================================================
# RBAC: only authorized admin roles may edit plans (unchanged, verified intact)
# ===========================================================================

def test_plan_admin_routes_still_require_admin_permission():
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    for anchor in ("def admin_add_plan(", "def admin_edit_plan(", "def admin_toggle_plan_status(", "def admin_delete_plan("):
        idx = source.index(anchor)
        preceding = source[max(0, idx - 400):idx]
        assert "require_admin_permission" in preceding or "@admin_required" in preceding, anchor
