import io
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash


def _login_admin(client, admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id


def _payment(app_module, db_session, normal_user, plan, index=0, status="success"):
    payment = app_module.PaymentOrder(
        order_id=f"order-repair-{index}",
        user_id=normal_user.id,
        plan_id=plan.id,
        amount=100,
        total_amount=100,
        status=status,
        payment_method="card",
        razorpay_order_id=f"rzp-order-{index}",
        razorpay_payment_id=f"rzp-pay-{index}",
        subscription_start=datetime.utcnow(),
        subscription_end=datetime.utcnow() + timedelta(days=30),
        purchased_project_limit=3,
        purchased_scan_limit=50,
        payment_at=datetime.utcnow(),
    )
    db_session.add(payment)
    db_session.commit()
    return payment


def test_subscriptions_and_activity_logs_render(client, app_module, db_session, admin, normal_user, plan):
    _login_admin(client, admin)
    _payment(app_module, db_session, normal_user, plan)
    app_module.log_admin_activity(admin.id, "test_event", "Rendered activity log smoke")

    subscriptions = client.get("/admin/subscriptions")
    activity = client.get("/admin/activity-logs")

    assert subscriptions.status_code == 200
    assert b"Subscriptions" in subscriptions.data
    assert b"order-repair-0" in subscriptions.data
    assert activity.status_code == 200
    assert b"Activity Logs" in activity.data
    assert b"Rendered activity log smoke" in activity.data


def test_important_authenticated_admin_get_routes_render(client, app_module, db_session, admin, normal_user, plan, project_with_pair):
    _login_admin(client, admin)
    payment = _payment(app_module, db_session, normal_user, plan)
    project, _pair = project_with_pair

    for path in (
        "/admin/dashboard",
        "/admin/users",
        "/admin/projects",
        f"/admin/projects/{project.id}",
        "/admin/plans",
        "/admin/subscriptions",
        "/admin/payments",
        f"/admin/payments/{payment.id}",
        "/admin/activity-logs",
        "/admin/settings",
    ):
        response = client.get(path)
        assert response.status_code == 200, path


def test_suspended_project_blocks_and_restore_reenables_scanner_and_media(client, app_module, db_session, admin, project_with_pair):
    _login_admin(client, admin)
    project, pair = project_with_pair

    suspend = client.post(f"/admin/projects/{project.id}/suspend", follow_redirects=True)
    assert suspend.status_code == 200
    assert app_module.Project.query.get(project.id).is_active is False

    scanner = client.get(f"/scanner/{project.id}")
    detect = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (io.BytesIO(b"not-image"), "frame.jpg")},
        content_type="multipart/form-data",
    )

    assert scanner.status_code == 404
    assert b"suspended or unavailable" in scanner.data
    assert detect.status_code == 404
    assert detect.get_json()["reason"] == "Project is suspended or unavailable"

    # The suspension's job is to block the PUBLIC, which it still does.
    with client.session_transaction() as sess:
        sess.pop("admin_id", None)
    assert client.get(f"/video/{project.id}/{pair.pair_index}").status_code == 404
    assert client.get(f"/image/{project.id}/{pair.pair_index}").status_code == 404

    # ...but it must NOT blind the admin who ordered it. This assertion used to
    # read 404 for an admin session too, which encoded a real moderation defect:
    # the only way to review the evidence behind a report was to re-publish the
    # reported content first. See _admin_media_investigation_allowed.
    _login_admin(client, admin)
    admin_video = client.get(f"/video/{project.id}/{pair.pair_index}")
    admin_image = client.get(f"/image/{project.id}/{pair.pair_index}")
    assert admin_video.status_code == 200
    assert admin_image.status_code == 200
    assert admin_video.headers["Cache-Control"] == "private, no-store"
    assert admin_image.headers["Cache-Control"] == "private, no-store"

    restore = client.post(f"/admin/projects/{project.id}/restore", follow_redirects=True)
    assert restore.status_code == 200
    assert app_module.Project.query.get(project.id).is_active is True
    scanner_after_restore = client.get(f"/scanner/{project.id}", follow_redirects=False)
    assert scanner_after_restore.status_code == 302
    assert scanner_after_restore.headers["Location"].endswith(f"/s/{project.public_key}")
    assert client.get(f"/video/{project.id}/{pair.pair_index}").status_code == 200


def test_plain_user_cannot_invoke_admin_project_actions(client, login_user, project_with_pair):
    project, _pair = project_with_pair
    response = client.post(f"/admin/projects/{project.id}/suspend", follow_redirects=False)
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]


def test_state_changing_project_actions_reject_get(client, admin, project_with_pair):
    _login_admin(client, admin)
    project, _pair = project_with_pair
    for path in (
        f"/admin/projects/{project.id}/suspend",
        f"/admin/projects/{project.id}/restore",
        f"/admin/projects/{project.id}/toggle-status",
    ):
        assert client.get(path).status_code == 405


def test_refund_and_receipt_dead_links_are_not_clickable(client, app_module, db_session, admin, normal_user, plan):
    """Receipt resend is still a dead action and stays disabled. Refund is no
    longer one: it now points at a real endpoint, so the requirement is that the
    URL it advertises actually resolves rather than that it is absent."""
    _login_admin(client, admin)
    payment = _payment(app_module, db_session, normal_user, plan)
    response = client.get(f"/admin/payments/{payment.id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "resend-receipt" not in body
    assert "Receipt resend unavailable" in body

    refund_url = f"/admin/api/payments/{payment.id}/refund"
    assert f'data-refund-url="{refund_url}"' in body
    assert app_module.app.url_map.bind("localhost").match(refund_url, method="POST")[0] == "admin_refund_payment"


def test_failed_admin_login_is_logged_and_repeated_failures_lock_account(client, app_module, db_session, admin):
    admin.password_hash = generate_password_hash("RightPassword123")
    db_session.commit()

    for _ in range(app_module.ADMIN_LOGIN_LOCKOUT_MAX_ATTEMPTS):
        response = client.post("/admin/login", data={"email": admin.email, "password": "wrong"})
        assert response.status_code == 200

    locked = client.post("/admin/login", data={"email": admin.email, "password": "RightPassword123"})
    assert locked.status_code == 429
    assert b"Invalid email or password" in locked.data
    assert app_module.AdminActivity.query.filter_by(admin_id=admin.id, activity_type="login_failed").count() == app_module.ADMIN_LOGIN_LOCKOUT_MAX_ATTEMPTS
    state = app_module.get_system_config(app_module._admin_login_attempt_key(admin.email), {})
    assert state["count"] == app_module.ADMIN_LOGIN_LOCKOUT_MAX_ATTEMPTS
    assert state["locked_until"]


def test_pagination_bounds_results(client, app_module, db_session, admin, normal_user, plan):
    _login_admin(client, admin)
    for index in range(4):
        db_session.add(app_module.User(
            email=f"paged-{index}@example.com",
            password_hash=generate_password_hash("Password123"),
            is_verified=True,
            subscription_id=plan.id,
            subscription_status="trial",
        ))
        _payment(app_module, db_session, normal_user, plan, index=index)
        app_module.log_admin_activity(admin.id, "paged_event", f"Event {index}")
    db_session.commit()

    users_body = client.get("/admin/users?search=paged-&per_page=2").get_data(as_text=True)
    payments_body = client.get("/admin/payments?search=order-repair-&per_page=2").get_data(as_text=True)
    activity_body = client.get("/admin/activity-logs?activity_type=paged_event&per_page=2").get_data(as_text=True)

    assert sum(users_body.count(f"paged-{index}@example.com") for index in range(4)) <= 2
    assert sum(payments_body.count(f"order-repair-{index}") for index in range(4)) <= 2
    assert sum(activity_body.count(f"Event {index}") for index in range(4)) <= 2
