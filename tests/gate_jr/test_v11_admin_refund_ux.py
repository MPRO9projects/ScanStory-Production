"""V1.1 admin refund UI.

The refund BACKEND (routes, eligibility, provider call, reconciliation) is
covered by tests/integration/test_admin_refunds.py. This file only covers the
admin-facing surfaces that drive it, and the two invariants that are easy to
break by accident:

  * refund status and entitlement-reconciliation status are two INDEPENDENT
    axes. A Razorpay-successful refund whose entitlements still need a human
    must never be rendered as "Refund failed";
  * nothing anywhere offers a partial refund or a user self-service refund,
    because the backend supports neither.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash


TEMPLATES = Path("templates")


def read_template(name):
    return (TEMPLATES / name).read_text(encoding="utf-8", errors="ignore")


def login_admin(client, admin):
    with client.session_transaction() as sess:
        sess.clear()
        sess["admin_id"] = admin.id


def make_catalog(app_module, db_session, code, addon_type, **deltas):
    item = app_module.AddonCatalog(
        code=code,
        name=code,
        addon_type=addon_type,
        unit_amount=99.0,
        currency="INR",
        project_delta=deltas.get("project_delta"),
        scan_delta=deltas.get("scan_delta"),
        validity_days_delta=deltas.get("validity_days_delta"),
        is_active=True,
        is_commercially_available=True,
    )
    db_session.add(item)
    db_session.commit()
    return item


def make_fulfilled_purchase(app_module, db_session, user, item, *, project=None, suffix="1", payment_id="pay_ui_1"):
    purchase = app_module.AddonPurchase(
        order_id=f"ADDON_UI_{item.code}_{suffix}",
        user_id=user.id,
        catalog_id=item.id,
        project_id=project.id if project else None,
        quantity=1,
        amount=item.unit_amount,
        total_amount=item.unit_amount,
        currency=item.currency,
        status="pending",
        razorpay_order_id=f"order_ui_{item.code}_{suffix}",
        razorpay_payment_id=payment_id,
    )
    db_session.add(purchase)
    db_session.commit()
    assert app_module.fulfill_addon_purchase(purchase)["success"] is True
    db_session.refresh(purchase)
    return purchase


def make_payment_order(app_module, db_session, user, plan, *, status="success", payment_id="pay_ui_sub"):
    order = app_module.PaymentOrder(
        order_id=f"ORD_UI_{user.id}_{payment_id}",
        razorpay_order_id=f"order_ui_{user.id}_{payment_id}",
        razorpay_payment_id=payment_id,
        user_id=user.id,
        plan_id=plan.id,
        amount=plan.plan_amount,
        offer_amount=plan.offer_price,
        total_amount=plan.effective_price,
        currency=plan.currency,
        status=status,
        purchased_project_limit=plan.total_project_limit,
        purchased_scan_limit=plan.total_scan_limit,
        subscription_start=datetime.utcnow(),
        subscription_end=datetime.utcnow() + timedelta(days=30),
        payment_at=datetime.utcnow(),
    )
    db_session.add(order)
    db_session.commit()
    return order


def make_refund(app_module, db_session, admin, *, payment_order=None, addon_purchase=None,
                status="REFUND_REQUESTED", reconciliation_status="PENDING", **kwargs):
    refund = app_module.PaymentRefund(
        payment_order_id=payment_order.id if payment_order else None,
        addon_purchase_id=addon_purchase.id if addon_purchase else None,
        user_id=(payment_order or addon_purchase).user_id,
        provider="RAZORPAY",
        provider_payment_id=(payment_order or addon_purchase).razorpay_payment_id,
        amount=(payment_order or addon_purchase).total_amount,
        currency="INR",
        status=status,
        reconciliation_status=reconciliation_status,
        reason="Customer asked for a refund.",
        requested_by_admin_id=admin.id,
        requested_at=datetime.utcnow(),
        idempotency_key=f"refund:test:{status}:{reconciliation_status}:{kwargs.pop('key', '1')}",
        **kwargs,
    )
    db_session.add(refund)
    db_session.commit()
    return refund


def limited_admin(app_module, db_session, email="no-refund-admin@example.com"):
    """A real admin whose role genuinely lacks admin.payments.refund."""
    assert "admin.payments.refund" not in app_module.ADMIN_ROLE_PERMISSIONS["admin"]
    other = app_module.Admin(
        email=email,
        name="Limited Admin",
        password_hash=generate_password_hash("AdminPass123"),
        role="admin",
        is_active=True,
    )
    db_session.add(other)
    db_session.commit()
    return other


# ===========================================================================
# A. The real backend contract this UI is written against
# ===========================================================================
def test_refund_label_maps_cover_every_persisted_enum_value_exactly(app_module):
    from models import REFUND_RECONCILIATION_STATUSES, REFUND_STATUSES

    assert set(app_module.REFUND_STATUS_LABELS) == REFUND_STATUSES
    assert set(app_module.REFUND_RECONCILIATION_LABELS) == REFUND_RECONCILIATION_STATUSES
    assert app_module.REFUND_STATUS_LABELS["REFUND_REQUESTED"] == "Refund requested"
    assert app_module.REFUND_STATUS_LABELS["REFUND_PROCESSING"] == "Refund processing"
    assert app_module.REFUND_STATUS_LABELS["REFUNDED"] == "Refunded"
    assert app_module.REFUND_STATUS_LABELS["REFUND_FAILED"] == "Refund failed"
    assert app_module.REFUND_RECONCILIATION_LABELS["PENDING"] == "Entitlement update pending"
    assert app_module.REFUND_RECONCILIATION_LABELS["APPLIED"] == "Entitlements reconciled"
    assert app_module.REFUND_RECONCILIATION_LABELS["MANUAL_REVIEW_REQUIRED"] == "Manual reconciliation required"
    assert app_module.REFUND_RECONCILIATION_LABELS["FAILED"] == "Reconciliation needs attention"


def test_refund_permission_is_superadmin_only_and_high_impact(app_module):
    assert "admin.payments.refund" not in app_module.ADMIN_ROLE_PERMISSIONS["admin"]
    assert "admin.payments.refund" in app_module.ADMIN_ROLE_PERMISSIONS["superadmin"]
    assert "admin.payments.refund" in app_module.HIGH_IMPACT_PERMISSIONS


def test_refund_endpoints_accept_no_amount_field(app_module):
    """Full refunds only: the initiation path reads reason + idempotency_key and
    nothing else, and the provider call derives the amount from the source row."""
    import inspect

    for view in ("admin_refund_payment", "admin_refund_addon_purchase"):
        source = inspect.getsource(app_module.app.view_functions[view])
        assert 'payload.get("reason")' in source
        assert "amount" not in source
    provider_call = inspect.getsource(app_module._call_razorpay_full_refund)
    assert "_refund_amount_paise(refund.amount)" in provider_call


# ===========================================================================
# B. Who can see the action
# ===========================================================================
def test_authorized_admin_sees_the_refund_action_for_an_eligible_payment(
    client, app_module, db_session, normal_user, plan, admin
):
    order = make_payment_order(app_module, db_session, normal_user, plan)
    login_admin(client, admin)
    body = client.get(f"/admin/payments/{order.id}").get_data(as_text=True)

    assert "Refund Payment" in body
    assert f'data-refund-url="/admin/api/payments/{order.id}/refund"' in body
    assert 'id="refundReason"' in body
    assert "Refund not available" not in body


def test_unauthorized_admin_never_sees_the_refund_action(
    client, app_module, db_session, normal_user, plan, admin
):
    order = make_payment_order(app_module, db_session, normal_user, plan)
    login_admin(client, limited_admin(app_module, db_session))
    body = client.get(f"/admin/payments/{order.id}").get_data(as_text=True)

    assert "Refund Payment" not in body
    assert "data-refund-url" not in body
    assert 'id="refundReason"' not in body
    # Read-only refund state is still visible - only the action is withheld.
    assert "Original amount" in body


def test_refund_action_posts_to_the_real_backend_endpoint(
    client, app_module, db_session, normal_user, plan, admin
):
    """The URL is url_for'd from the real view, so a renamed route breaks here
    rather than silently 404-ing in production."""
    order = make_payment_order(app_module, db_session, normal_user, plan)
    login_admin(client, admin)
    body = client.get(f"/admin/payments/{order.id}").get_data(as_text=True)

    with app_module.app.test_request_context():
        from flask import url_for

        expected = url_for("admin_refund_payment", payment_id=order.id)
    assert f'data-refund-url="{expected}"' in body
    assert expected == f"/admin/api/payments/{order.id}/refund"


# ===========================================================================
# C. Eligibility comes from the backend, never from the browser
# ===========================================================================
def test_ineligible_payment_shows_the_real_reason_and_no_active_button(
    client, app_module, db_session, normal_user, plan, admin
):
    order = make_payment_order(app_module, db_session, normal_user, plan, status="pending", payment_id="pay_ui_pending")
    login_admin(client, admin)
    body = client.get(f"/admin/payments/{order.id}").get_data(as_text=True)

    reason = app_module.refund_eligibility_for_payment_order(order)["reason_text"]
    assert reason == "Only successful paid orders can be refunded."
    assert reason in body
    assert "disabled" in body.split("Refund Payment")[0][-400:]
    assert "data-refund-url" not in body


def test_renewal_refund_ineligibility_is_taken_from_the_backend(
    client, app_module, db_session, normal_user, admin, project_with_pair
):
    """A standalone renewal whose coverage has already started is ineligible
    (INELIGIBLE_CONSUMED_SERVICE) - the row shows that reason and offers no
    action, and the UI does not second-guess it."""
    project, _pair = project_with_pair
    item = make_catalog(app_module, db_session, "REN_UI", "PROJECT_SERVICE_COVERAGE", validity_days_delta=365)
    purchase = make_fulfilled_purchase(
        app_module, db_session, normal_user, item, project=project, suffix="ren", payment_id="pay_ui_ren"
    )
    # A renewal bought ahead of time is refundable; one whose period has already
    # started is not. Move the coverage window into the past to reach the
    # already-consumed branch.
    coverage = app_module._coverage_for_addon_purchase(purchase)
    coverage.coverage_start = datetime.utcnow() - timedelta(days=1)
    db_session.commit()

    eligibility = app_module.refund_eligibility_for_addon_purchase(purchase)
    assert eligibility["eligible"] is False
    assert eligibility["reason_code"] == "INELIGIBLE_CONSUMED_SERVICE"
    assert eligibility["reason_text"] == "This renewal period has already started and requires manual review."

    login_admin(client, admin)
    # Money-moving refund actions were relocated from Operations to
    # Payments/Refunds (World-Class Admin Restructure, 2026-09-02) - this
    # test's own route/template followed that IA move.
    body = client.get("/admin/payments/refunds").get_data(as_text=True)
    assert f"Refund not available: {eligibility['reason_text']}" in body
    assert f'data-refund-url="/admin/api/addon-purchases/{purchase.id}/refund"' not in body


def test_eligible_addon_purchase_offers_the_action_with_a_required_reason(
    client, app_module, db_session, normal_user, admin
):
    item = make_catalog(app_module, db_session, "CAP_UI", "PROJECT_CAPACITY", project_delta=2)
    purchase = make_fulfilled_purchase(app_module, db_session, normal_user, item, suffix="cap", payment_id="pay_ui_cap")
    assert app_module.refund_eligibility_for_addon_purchase(purchase)["eligible"] is True

    login_admin(client, admin)
    body = client.get("/admin/payments/refunds").get_data(as_text=True)
    assert f'data-refund-url="/admin/api/addon-purchases/{purchase.id}/refund"' in body
    assert f'id="addonRefundReason{purchase.id}"' in body
    assert "required" in body.split(f'id="addonRefundReason{purchase.id}"')[0][-300:]


# ===========================================================================
# D. The two status axes stay separate
# ===========================================================================
@pytest.mark.parametrize(
    "status,label",
    [
        ("REFUND_REQUESTED", "Refund requested"),
        ("REFUND_PROCESSING", "Refund processing"),
        ("REFUNDED", "Refunded"),
        ("REFUND_FAILED", "Refund failed"),
    ],
)
def test_every_refund_status_renders_distinctly(
    client, app_module, db_session, normal_user, plan, admin, status, label
):
    order = make_payment_order(app_module, db_session, normal_user, plan, payment_id=f"pay_ui_{status.lower()}")
    make_refund(app_module, db_session, admin, payment_order=order, status=status, key=status)
    login_admin(client, admin)
    body = client.get(f"/admin/payments/{order.id}").get_data(as_text=True)

    marker = f'data-refund-status="{status}"'
    assert marker in body
    # The status line renders this status' label and no other status' label.
    rendered = body.split(marker, 1)[1].split("</span>", 1)[0]
    assert label in rendered
    others = {v for k, v in app_module.REFUND_STATUS_LABELS.items() if k != status}
    assert not [other for other in others if other in rendered]
    # Exactly one refund-status axis on the page, and one reconciliation axis.
    assert body.count("data-refund-status=") == 1
    assert body.count("data-reconciliation-status=") == 1


def test_reconciliation_status_is_a_separate_visible_fact_from_refund_status(
    client, app_module, db_session, normal_user, plan, admin
):
    """Razorpay succeeded; entitlements still need a human. Both facts show,
    and the transaction is NOT labelled as a failed refund."""
    order = make_payment_order(app_module, db_session, normal_user, plan, payment_id="pay_ui_manual")
    make_refund(
        app_module, db_session, admin,
        payment_order=order,
        status="REFUNDED",
        reconciliation_status="MANUAL_REVIEW_REQUIRED",
        reconciliation_message_safe=app_module.REFUND_MANUAL_SUBSCRIPTION_MESSAGE,
        key="manual",
    )
    login_admin(client, admin)
    body = client.get(f"/admin/payments/{order.id}").get_data(as_text=True)

    assert 'data-refund-status="REFUNDED"' in body
    assert 'data-reconciliation-status="MANUAL_REVIEW_REQUIRED"' in body
    assert "Refunded" in body
    assert "Manual reconciliation required" in body
    assert app_module.REFUND_MANUAL_SUBSCRIPTION_MESSAGE in body
    assert "Refund failed" not in body
    # Never claim a subscription was cancelled - no such backend state exists.
    assert "Subscription cancelled" not in body
    assert "subscription cancelled" not in body.lower()


def test_reconciliation_labels_are_rendered_from_the_map_not_raw_codes(
    client, app_module, db_session, normal_user, plan, admin
):
    order = make_payment_order(app_module, db_session, normal_user, plan, payment_id="pay_ui_applied")
    make_refund(
        app_module, db_session, admin,
        payment_order=order, status="REFUNDED", reconciliation_status="APPLIED", key="applied",
    )
    login_admin(client, admin)
    body = client.get(f"/admin/payments/{order.id}").get_data(as_text=True)

    assert "Entitlements reconciled" in body
    # The raw code only ever appears as a machine-readable data attribute.
    assert body.count("APPLIED") == body.count('data-reconciliation-status="APPLIED"')


# ===========================================================================
# E. Confirmation copy, honest capacity wording, no partial refunds
# ===========================================================================
def test_confirmation_copy_states_full_refund_only_and_is_used_verbatim():
    notice = read_template("admin/view_payment.html")
    assert "REFUND_CONFIRMATION_NOTICE" in notice
    from app import REFUND_CONFIRMATION_NOTICE

    assert "FULL refund" in REFUND_CONFIRMATION_NOTICE
    assert "Partial refunds are not supported in V1.1" in REFUND_CONFIRMATION_NOTICE
    assert "Razorpay will process the refund" in REFUND_CONFIRMATION_NOTICE
    assert "may not be instant" in REFUND_CONFIRMATION_NOTICE
    assert "never deleted automatically" in REFUND_CONFIRMATION_NOTICE
    assert "manual entitlement reconciliation" in REFUND_CONFIRMATION_NOTICE
    for template in ("admin/view_payment.html", "admin/payments_refunds.html"):
        html = read_template(template)
        assert "window.confirm(NOTICE" in html


def test_the_notice_is_visible_before_submission_not_only_in_the_dialog(
    client, app_module, db_session, normal_user, plan, admin
):
    order = make_payment_order(app_module, db_session, normal_user, plan, payment_id="pay_ui_notice")
    login_admin(client, admin)
    body = client.get(f"/admin/payments/{order.id}").get_data(as_text=True)
    assert "Partial refunds are not supported in V1.1" in body


def test_no_partial_refund_input_exists_in_any_refund_surface():
    for template in ("admin/view_payment.html", "admin/payments_refunds.html"):
        html = read_template(template).lower()
        block = html.split("refund", 1)[1]
        for offender in ("partial refund", 'name="amount"', 'id="refundamount"', "refund amount input", 'type="number"'):
            assert offender not in block, (template, offender)


def test_capacity_refund_copy_never_implies_deletion(app_module):
    note = app_module.REFUND_ENTITLEMENT_EFFECT_NOTES["PROJECT_CAPACITY"]
    for forbidden in ("delete", "removed project", "will be removed", "lost", "erase"):
        assert forbidden not in note.lower()
    assert "kept and keep working" in note
    assert "new ScanStory creation and incoming transfers stay unavailable" in note
    for text in app_module.REFUND_ENTITLEMENT_EFFECT_NOTES.values():
        assert "delete" not in text.lower()
    assert "never deleted automatically" in app_module.REFUND_CONFIRMATION_NOTICE


def test_capacity_refund_copy_is_shown_on_an_eligible_capacity_purchase(
    client, app_module, db_session, normal_user, admin
):
    item = make_catalog(app_module, db_session, "CAP_COPY", "PROJECT_CAPACITY", project_delta=2)
    make_fulfilled_purchase(app_module, db_session, normal_user, item, suffix="copy", payment_id="pay_ui_capcopy")
    login_admin(client, admin)
    body = client.get("/admin/payments/refunds").get_data(as_text=True)
    assert "Existing ScanStorys, media and QR codes are kept and keep working." in body
    # payments_refunds.html has no "Recent Entitlement Ledger" section after
    # this one (that stayed on Operations as a read-only diagnostic table) -
    # this card is the last one on the page, so slice to the script block.
    addon_section = body.split("Recent Add-on Purchases", 1)[1].split("<script>", 1)[0].lower()
    # "never deleted automatically" is the only permitted use of the word.
    assert addon_section.count("deleted") == addon_section.count("never deleted automatically")
    for forbidden in ("will be deleted", "removed project", "projects removed", "erase"):
        assert forbidden not in addon_section


# ===========================================================================
# F. Post-action behaviour and the user-facing boundary
# ===========================================================================
def test_refund_submission_requires_a_reason_before_it_can_be_sent():
    for template in ("admin/view_payment.html", "admin/payments_refunds.html"):
        html = read_template(template)
        assert "A refund reason is required." in html
        guard = html.split("A refund reason is required.")[0]
        # The guard returns before any fetch is issued.
        assert guard.rindex("if (!reason)") > guard.rindex("fetch(") if "fetch(" in guard else True
        assert 'required' in html


def test_the_page_refetches_authoritative_state_instead_of_trusting_the_response():
    for template in ("admin/view_payment.html", "admin/payments_refunds.html"):
        html = read_template(template)
        after = html.split("btn.dataset.refundUrl", 1)[1]
        assert "window.location.reload()" in after
        # The response body is never painted as the final refund status.
        assert "payload.refund" not in after
        assert "data.refund" not in after


def test_no_user_self_service_refund_is_reachable_by_a_normal_user(app_module):
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        if "admin" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for phrase in ("request refund", "request a refund", "refund payment", "refund purchase", "issue refund"):
            if phrase in text:
                offenders.append((str(path), phrase))
    assert offenders == []

    refund_rules = [rule for rule in app_module.app.url_map.iter_rules() if "refund" in str(rule).lower()]
    assert refund_rules
    for rule in refund_rules:
        assert str(rule).startswith("/admin/")


def test_normal_user_cannot_reach_any_refund_surface(client, app_module, db_session, normal_user, plan, admin):
    order = make_payment_order(app_module, db_session, normal_user, plan, payment_id="pay_ui_user")
    with client.session_transaction() as sess:
        sess.clear()
        sess["user_id"] = normal_user.id
    for path in (
        f"/admin/payments/{order.id}",
        "/admin/operations",
        f"/admin/api/payments/{order.id}/refund",
    ):
        response = client.get(path)
        assert response.status_code != 200 or "Refund Payment" not in response.get_data(as_text=True)
