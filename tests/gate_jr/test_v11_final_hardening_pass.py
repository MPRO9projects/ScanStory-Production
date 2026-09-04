"""Final Product Completeness Pass (2026-09-03) - Lanes B/C/D/E/G.

Lane A (entitlement ledger) tests live in
tests/integration/test_wave2_entitlement_foundation.py, next to the rest of
the entitlement-ledger coverage they extend.
"""
from pathlib import Path

from werkzeug.security import generate_password_hash


LANDING_TEMPLATE = Path("templates/user/landing.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Lane G: public landing desktop CTA defect
# ---------------------------------------------------------------------------
def test_hero_section_has_explicit_stacking_above_next_section():
    """Root cause (confirmed live via Playwright at 1366x768/1440x900): both
    #hero-section (min-height:100vh, margin-bottom:-60px, a deliberate
    section-seam overlap) and #how-it-works stacked at the same default
    level, so DOM order alone decided the winner in that overlap strip -
    #how-it-works (the later element) painted on top and silently ate clicks
    meant for the hero CTA whenever it sat close to that -60px zone (short
    desktop viewport heights; portrait phones are tall enough to avoid it,
    which is why mobile "just worked"). Fixed via z-index only - no visual
    change, no redesign."""
    idx = LANDING_TEMPLATE.index("#hero-section {")
    block = LANDING_TEMPLATE[idx:idx + 1200]
    assert "margin-bottom: -60px;" in block
    assert "z-index: 1;" in block


def test_how_it_works_section_has_no_competing_z_index():
    idx = LANDING_TEMPLATE.index("#how-it-works {")
    block = LANDING_TEMPLATE[idx:idx + 200]
    assert "z-index" not in block


# ---------------------------------------------------------------------------
# Lane B: Suspend/Restore is Super Admin-only platform moderation
# ---------------------------------------------------------------------------
def _regular_admin(app_module, db_session, email="regular-mod@example.com"):
    a = app_module.Admin(
        email=email, password_hash=generate_password_hash("AdminPass123"),
        role="admin", is_active=True,
    )
    db_session.add(a)
    db_session.commit()
    return a


def _project(app_module, db_session, *, owner_user=None, owner_admin=None, name="Mod Target"):
    project = app_module.Project(
        name=name, owner_user_id=owner_user.id if owner_user else None,
        owner_admin_id=owner_admin.id if owner_admin else None,
        experience_type="image_video", playback_mode="tracked_overlay", is_active=True,
    )
    db_session.add(project)
    db_session.commit()
    return project


def test_regular_admin_denied_suspend_on_customer_project(app_module, db_session, client, normal_user):
    project = _project(app_module, db_session, owner_user=normal_user)
    regular = _regular_admin(app_module, db_session)
    with client.session_transaction() as sess:
        sess["admin_id"] = regular.id
    resp = client.post(f"/admin/projects/{project.id}/suspend", data={"reason": "should be denied"})
    assert resp.status_code != 200
    assert app_module.Project.query.get(project.id).is_active is True


def test_regular_admin_denied_suspend_on_another_admins_project(app_module, db_session, client, admin):
    other_admin_project = _project(app_module, db_session, owner_admin=admin)
    regular = _regular_admin(app_module, db_session, email="regular-mod-2@example.com")
    with client.session_transaction() as sess:
        sess["admin_id"] = regular.id
    resp = client.post(f"/admin/projects/{other_admin_project.id}/suspend", data={"reason": "should be denied"})
    assert resp.status_code != 200
    assert app_module.Project.query.get(other_admin_project.id).is_active is True


def test_regular_admin_denied_suspend_on_own_project(app_module, db_session, client):
    """Suspend/Restore is moderation, not Creator management - a regular
    Admin keeps Preview/Edit/Delete/etc on their own project but not this,
    even for a project they own themselves."""
    regular = _regular_admin(app_module, db_session, email="regular-mod-3@example.com")
    own_project = _project(app_module, db_session, owner_admin=regular)
    with client.session_transaction() as sess:
        sess["admin_id"] = regular.id
    resp = client.post(f"/admin/projects/{own_project.id}/suspend", data={"reason": "should be denied"})
    assert resp.status_code != 200
    assert app_module.Project.query.get(own_project.id).is_active is True


def test_superadmin_can_suspend_and_restore_with_reason(app_module, db_session, client, admin, normal_user):
    project = _project(app_module, db_session, owner_user=normal_user)
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id

    resp = client.post(f"/admin/projects/{project.id}/suspend", data={"reason": "policy violation reported"})
    assert resp.status_code in (302, 303)
    assert app_module.Project.query.get(project.id).is_active is False
    activity = app_module.AdminActivity.query.filter_by(activity_type="project_suspend").order_by(
        app_module.AdminActivity.id.desc()
    ).first()
    assert activity is not None and "policy violation reported" in activity.description

    resp = client.post(f"/admin/projects/{project.id}/restore", data={"reason": "appeal accepted"})
    assert resp.status_code in (302, 303)
    assert app_module.Project.query.get(project.id).is_active is True
    activity = app_module.AdminActivity.query.filter_by(activity_type="project_restore").order_by(
        app_module.AdminActivity.id.desc()
    ).first()
    assert activity is not None and "appeal accepted" in activity.description


def test_superadmin_suspend_requires_reason(app_module, db_session, client, admin, normal_user):
    project = _project(app_module, db_session, owner_user=normal_user)
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id
    resp = client.post(f"/admin/projects/{project.id}/suspend", data={"reason": ""})
    assert resp.status_code in (302, 303)
    assert app_module.Project.query.get(project.id).is_active is True


def test_view_project_hides_suspend_restore_for_regular_admin(app_module, db_session, client):
    regular = _regular_admin(app_module, db_session, email="regular-mod-4@example.com")
    own_project = _project(app_module, db_session, owner_admin=regular)
    with client.session_transaction() as sess:
        sess["admin_id"] = regular.id
    resp = client.get(f"/admin/projects/{own_project.id}")
    assert resp.status_code == 200
    assert b"Suspend Project" not in resp.data
    assert b"Restore Project" not in resp.data


def test_view_project_shows_suspend_for_superadmin(app_module, db_session, client, admin, normal_user):
    project = _project(app_module, db_session, owner_user=normal_user)
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id
    resp = client.get(f"/admin/projects/{project.id}")
    assert resp.status_code == 200
    assert b"Suspend Project" in resp.data


# ---------------------------------------------------------------------------
# Lane C: authenticated Change Password (User + Admin)
# ---------------------------------------------------------------------------
def test_user_change_password_success_and_old_password_fails(app_module, db_session, client, normal_user, monkeypatch):
    monkeypatch.setattr(app_module, "send_password_changed_email", lambda *a, **k: None)
    normal_user.password_hash = generate_password_hash("OldPass123")
    db_session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id

    resp = client.post("/profile/change-password", data={
        "current_password": "OldPass123", "new_password": "NewPass456", "confirm_password": "NewPass456",
    })
    assert resp.status_code in (302, 303)

    user = app_module.User.query.get(normal_user.id)
    assert app_module.check_password_hash(user.password_hash, "NewPass456")
    assert not app_module.check_password_hash(user.password_hash, "OldPass123")

    with client.session_transaction() as sess:
        sess.clear()
    client.post("/login/", data={"email": user.email, "password": "OldPass123"})
    with client.session_transaction() as sess:
        assert "user_id" not in sess  # old password no longer authenticates

    client.post("/login/", data={"email": user.email, "password": "NewPass456"})
    with client.session_transaction() as sess:
        assert sess.get("user_id") == user.id  # new password authenticates


def test_user_change_password_rejects_wrong_current_password(app_module, db_session, client, normal_user):
    normal_user.password_hash = generate_password_hash("OldPass123")
    db_session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id
    resp = client.post("/profile/change-password", data={
        "current_password": "WrongPass", "new_password": "NewPass456", "confirm_password": "NewPass456",
    })
    assert resp.status_code in (302, 303)
    user = app_module.User.query.get(normal_user.id)
    assert app_module.check_password_hash(user.password_hash, "OldPass123")


def test_user_change_password_rejects_mismatch_and_same_as_current(app_module, db_session, client, normal_user):
    normal_user.password_hash = generate_password_hash("OldPass123")
    db_session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id

    mismatch = client.post("/profile/change-password", data={
        "current_password": "OldPass123", "new_password": "NewPass456", "confirm_password": "Different789",
    })
    assert mismatch.status_code in (302, 303)
    assert app_module.check_password_hash(app_module.User.query.get(normal_user.id).password_hash, "OldPass123")

    same = client.post("/profile/change-password", data={
        "current_password": "OldPass123", "new_password": "OldPass123", "confirm_password": "OldPass123",
    })
    assert same.status_code in (302, 303)
    assert app_module.check_password_hash(app_module.User.query.get(normal_user.id).password_hash, "OldPass123")


def test_user_change_password_email_failure_does_not_undo_change(app_module, db_session, client, normal_user, monkeypatch):
    normal_user.password_hash = generate_password_hash("OldPass123")
    db_session.commit()

    def _boom(*a, **k):
        raise RuntimeError("SMTP down")
    monkeypatch.setattr(app_module, "send_password_changed_email", _boom)

    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id
    resp = client.post("/profile/change-password", data={
        "current_password": "OldPass123", "new_password": "NewPass456", "confirm_password": "NewPass456",
    })
    assert resp.status_code in (302, 303)
    assert app_module.check_password_hash(app_module.User.query.get(normal_user.id).password_hash, "NewPass456")


def test_admin_change_password_success_and_audited(app_module, db_session, client, admin, monkeypatch):
    monkeypatch.setattr(app_module, "send_password_changed_email", lambda *a, **k: None)
    admin.password_hash = generate_password_hash("OldAdminPass1")
    db_session.commit()
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id

    resp = client.post("/admin/account/change-password", data={
        "current_password": "OldAdminPass1", "new_password": "NewAdminPass2", "confirm_password": "NewAdminPass2",
    })
    assert resp.status_code in (302, 303)
    updated = app_module.Admin.query.get(admin.id)
    assert app_module.check_password_hash(updated.password_hash, "NewAdminPass2")
    activity = app_module.AdminActivity.query.filter_by(activity_type="self_password_change").order_by(
        app_module.AdminActivity.id.desc()
    ).first()
    assert activity is not None and activity.admin_id == admin.id


def test_admin_change_password_rejects_short_password(app_module, db_session, client, admin):
    admin.password_hash = generate_password_hash("OldAdminPass1")
    db_session.commit()
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id
    resp = client.post("/admin/account/change-password", data={
        "current_password": "OldAdminPass1", "new_password": "short1", "confirm_password": "short1",
    })
    assert resp.status_code in (302, 303)
    assert app_module.check_password_hash(app_module.Admin.query.get(admin.id).password_hash, "OldAdminPass1")


def test_change_password_link_present_in_admin_account_menu(app_module, db_session, client, admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id
    resp = client.get("/admin/dashboard")
    assert resp.status_code == 200
    assert b"Change Password" in resp.data


# ---------------------------------------------------------------------------
# Lane D: processing completion/failure communication
# ---------------------------------------------------------------------------
def _image_video_project(app_module, db_session, user, pair_count=1):
    project = app_module.Project(
        name="Notify Target", owner_user_id=user.id,
        experience_type="image_video", playback_mode="tracked_overlay", is_active=True,
    )
    db_session.add(project)
    db_session.commit()
    pairs = []
    for i in range(pair_count):
        pair = app_module.ProjectPair(
            project_id=project.id, pair_index=i,
            image_filename=f"{project.id}_{i}.jpg", video_filename=f"{project.id}_{i}.mp4",
            is_processed=False, processing_status="processing", feature_extraction_status="extracting",
        )
        db_session.add(pair)
        pairs.append(pair)
    db_session.commit()
    return project, pairs


def _processing_job(app_module, db_session, project, max_attempts=1):
    from processing_queue import create_processing_job
    job, _created = create_processing_job(
        "process_project_pairs", project_id=project.id,
        owner_user_id=project.owner_user_id, owner_admin_id=project.owner_admin_id,
        max_attempts=max_attempts,
    )
    return job


def test_processing_ready_email_sent_once_after_all_pairs_complete(app_module, db_session, normal_user, monkeypatch):
    import processing_operations

    project, pairs = _image_video_project(app_module, db_session, normal_user, pair_count=2)
    job = _processing_job(app_module, db_session, project)

    sent = []
    monkeypatch.setattr(app_module, "send_processing_ready_email", lambda user, proj: sent.append((user.id, proj.id)))
    monkeypatch.setattr(processing_operations, "_process_pair", lambda app_mod, proj, pair: {})

    result = processing_operations.run_processing_job(job.id)
    assert result["ok"] is True
    assert sent == [(normal_user.id, project.id)]
    assert app_module.ProjectPair.query.filter_by(project_id=project.id, is_processed=True).count() == 2


def test_processing_failed_email_sent_only_after_final_non_retryable_failure(app_module, db_session, normal_user, monkeypatch):
    import processing_operations

    project, pairs = _image_video_project(app_module, db_session, normal_user, pair_count=1)
    job = _processing_job(app_module, db_session, project, max_attempts=1)

    sent = []
    monkeypatch.setattr(app_module, "send_processing_failed_email", lambda user, proj: sent.append((user.id, proj.id)))
    monkeypatch.setattr(app_module, "send_processing_ready_email", lambda *a, **k: None)

    def _boom(app_mod, proj, pair):
        raise RuntimeError("feature extraction blew up")
    monkeypatch.setattr(processing_operations, "_process_pair", _boom)

    result = processing_operations.run_processing_job(job.id)
    db_session.expire_all()  # run_processing_job commits via its own app-context session
    assert result["ok"] is False
    assert sent == [(normal_user.id, project.id)]
    assert app_module.ProcessingJob.query.get(job.id).status == "failed"


def test_processing_failed_email_not_sent_while_job_still_retrying(app_module, db_session, normal_user, monkeypatch):
    import processing_operations

    project, pairs = _image_video_project(app_module, db_session, normal_user, pair_count=1)
    job = _processing_job(app_module, db_session, project, max_attempts=5)

    sent = []
    monkeypatch.setattr(app_module, "send_processing_failed_email", lambda user, proj: sent.append((user.id, proj.id)))

    def _boom(app_mod, proj, pair):
        raise RuntimeError("transient failure")
    monkeypatch.setattr(processing_operations, "_process_pair", _boom)

    result = processing_operations.run_processing_job(job.id)
    db_session.expire_all()  # run_processing_job commits via its own app-context session
    assert result["ok"] is False
    assert sent == []  # still retrying (attempt 1 of 5) - no email yet
    assert app_module.ProcessingJob.query.get(job.id).status == "retrying"


def test_processing_email_skipped_for_admin_owned_project(app_module, db_session, admin, monkeypatch):
    import processing_operations

    project = app_module.Project(
        name="Admin Notify Target", owner_admin_id=admin.id,
        experience_type="image_video", playback_mode="tracked_overlay", is_active=True,
    )
    db_session.add(project)
    db_session.commit()
    pair = app_module.ProjectPair(
        project_id=project.id, pair_index=0,
        image_filename=f"{project.id}_0.jpg", video_filename=f"{project.id}_0.mp4",
        is_processed=False, processing_status="processing", feature_extraction_status="extracting",
    )
    db_session.add(pair)
    db_session.commit()
    job = _processing_job(app_module, db_session, project)

    sent = []
    monkeypatch.setattr(app_module, "send_processing_ready_email", lambda user, proj: sent.append(True))
    monkeypatch.setattr(processing_operations, "_process_pair", lambda app_mod, proj, p: {})

    result = processing_operations.run_processing_job(job.id)
    assert result["ok"] is True
    assert sent == []


# ---------------------------------------------------------------------------
# Lane E: ownership email production branding
# ---------------------------------------------------------------------------
def test_ownership_notification_uses_branded_template_and_escapes_project_name(app_module, db_session, normal_user, monkeypatch):
    captured = {}
    monkeypatch.setattr(app_module, "send_email_smtp", lambda to, subject, html: captured.update(to=to, subject=subject, html=html))

    class _Recipient:
        email = normal_user.email

    with app_module.app.test_request_context():
        app_module._notify_ownership(
            _Recipient(),
            "A ScanStory is being handed over to you",
            'attacker@example.com has offered to transfer the ScanStory "<script>alert(1)</script>" to your account.',
        )
    assert captured["html"]
    assert "SCANSTORY" in captured["html"]  # branded shell, not the old bare <p>
    assert "<script>alert(1)</script>" not in captured["html"]  # escaped, not raw
    assert "&lt;script&gt;" in captured["html"]


def test_ownership_notification_skips_send_when_no_email(app_module, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "send_email_smtp", lambda *a, **k: calls.append(True))

    class _NoEmail:
        email = ""

    app_module._notify_ownership(_NoEmail(), "subject", "message")
    assert calls == []


# ---------------------------------------------------------------------------
# Final Pre-Freeze Closure Pass, Lane E: sidebar short-viewport fade
# ---------------------------------------------------------------------------
def test_sidebar_fade_element_and_js_present(client, login_admin):
    resp = client.get("/admin/dashboard")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'class="sidebar-fade"' in body
    assert 'id="sidebarScroll"' in body
    assert "sidebar.classList.toggle('at-bottom', atBottom)" in body


# ---------------------------------------------------------------------------
# Final Pre-Freeze Closure Pass, Lane B: currency must not be hardcoded to
# INR/rupee-symbol - a Plan/Addon can carry a non-INR currency value via the
# admin create/edit forms, and this page must reflect the real order.
# ---------------------------------------------------------------------------
def test_payment_success_page_uses_order_currency_not_hardcoded_inr(app_module, db_session, client, normal_user, plan):
    order = app_module.PaymentOrder(
        order_id="ORD_CUR_1", razorpay_order_id="rzp_cur_1", user_id=normal_user.id, plan_id=plan.id,
        amount=100.0, total_amount=100.0, currency="USD", status="success",
    )
    db_session.add(order)
    db_session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id
    resp = client.get("/payment-success", query_string={"order_id": "ORD_CUR_1"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "USD" in body
    assert "₹100.00" not in body
    assert "₹" not in body.split("Payment Details", 1)[1]


def test_payment_success_page_still_shows_inr_symbol_for_inr_orders(app_module, db_session, client, normal_user, plan):
    order = app_module.PaymentOrder(
        order_id="ORD_CUR_2", razorpay_order_id="rzp_cur_2", user_id=normal_user.id, plan_id=plan.id,
        amount=100.0, total_amount=100.0, currency="INR", status="success",
    )
    db_session.add(order)
    db_session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id
    resp = client.get("/payment-success", query_string={"order_id": "ORD_CUR_2"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "₹100.00" in body


def test_processing_ready_email_renders_outside_request_context(app_module, db_session, normal_user, monkeypatch):
    """Live-caught bug: run_processing_job (processing_operations.py) sends
    this email from inside the RQ worker's app-context-only scope - no real
    HTTP request, and url_for(..., _external=True) in the template needs one
    (or an equivalent) to build any URL at all. Reproduces the exact
    no-request-context scenario directly, with no ambient test_request_context
    a Flask test client would normally provide."""
    captured = {}
    monkeypatch.setattr(app_module, "send_email_smtp", lambda to, subject, html: captured.update(html=html))
    project = app_module.Project(
        name="No Request Context Target", owner_user_id=normal_user.id,
        experience_type="image_video", playback_mode="tracked_overlay", is_active=True,
    )
    db_session.add(project)
    db_session.commit()
    app_module.send_processing_ready_email(normal_user, project)  # must not raise
    assert captured.get("html")


def test_processing_failed_email_renders_outside_request_context(app_module, db_session, normal_user, monkeypatch):
    captured = {}
    monkeypatch.setattr(app_module, "send_email_smtp", lambda to, subject, html: captured.update(html=html))
    project = app_module.Project(
        name="No Request Context Target 2", owner_user_id=normal_user.id,
        experience_type="image_video", playback_mode="tracked_overlay", is_active=True,
    )
    db_session.add(project)
    db_session.commit()
    app_module.send_processing_failed_email(normal_user, project)  # must not raise
    assert captured.get("html")


def test_trial_settings_tab_and_form_fully_removed(client, login_admin):
    """Narrow Admin UI Cleanup: Trial Settings tab removed from Admin
    Settings - Plans (the Free Trial plan's own edit form) is now the only
    place an Admin can see/change trial configuration. Checks the actual
    nav link and form markup, not a bare substring search - an HTML comment
    explaining the removal legitimately contains the phrase "Trial Settings"
    too, which a naive `"Trial Settings" in body` check would false-positive
    on."""
    body = client.get("/admin/settings").data.decode()
    assert '#trial' not in body
    assert 'id="trialTab"' not in body
    assert 'name="free_trial_projects"' not in body
    assert 'name="free_trial_scans"' not in body
    assert 'name="free_trial_days"' not in body
    assert 'id="trialForm"' not in body


def test_settings_page_hash_restore_no_longer_references_trial_tab():
    html = Path("templates/admin/settings.html").read_text(encoding="utf-8")
    assert "'trial'" not in html


def test_addons_toggle_buttons_have_explicit_color_class(app_module, db_session, client, login_admin):
    """Live-reproduced: both toggle buttons were bare `class="btn btn-sm"`
    with no color modifier, so they fell through to Bootstrap's raw default
    body-text color (near-black, rgb(33,37,41)) on a transparent background
    over this near-black theme - readable-looking only once :hover happened
    to shift something. Computed style confirmed identical rest/hover
    otherwise. Fixed by giving both an explicit warning/success modifier
    matching the same convention Plans' own Activate/Deactivate already
    uses."""
    item = app_module.AddonCatalog(
        code="qa-lane1-addon", name="QA Lane1 Addon", addon_type="EXTRA_SCANS",
        unit_amount=10.0, currency="INR", scan_delta=100,
        is_active=True, is_commercially_available=True,
    )
    db_session.add(item)
    db_session.commit()
    body = client.get("/admin/addons").data.decode()
    assert 'class="btn btn-sm"' not in body
    assert "btn-warning" in body or "btn-success" in body


def test_create_project_upload_columns_share_action_slot_class():
    """Lane C: the photo column's 'Take a photo' button had no equivalent on
    the video column, so the two .upload-area boxes started at different
    vertical offsets in the same upload-grid row - reproduced live on both
    Admin and Creator Create Project (they share this template), confirmed
    fixed with a 0.0px bounding-box difference. Both columns now reserve a
    shared .pair-upload-action-slot instead of a per-card pixel offset."""
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8")
    assert html.count('class="pair-upload-action-slot') == 2  # one real, one empty spacer
    idx = html.index('class="pair-upload-action-slot')
    assert "image-source-camera-btn" in html[idx:idx + 300]


def test_dashboard_overview_cards_share_one_row_not_four(client, login_admin):
    """Lane A: the four overview KPI cards used to each sit alone in their
    own full-width row (75% of every row empty, forcing them to stack the
    height of a full screen) - now share one row."""
    body = client.get("/admin/dashboard").data.decode()
    overview_idx = body.index('dashboard-section-label">Overview')
    workspace_idx = body.index("Recent Users")
    section = body[overview_idx:workspace_idx]
    assert section.count('class="row mb-4"') == 1
    assert section.count("col-xl-3 col-md-6 mb-4") == 4


def test_dashboard_recent_users_table_rows_are_well_formed(client, login_admin):
    body = client.get("/admin/dashboard").data.decode()
    thead = body[body.index("<thead>"):body.index("</thead>")]
    assert "<tr>" in thead and "</tr>" in thead
    tbody = body[body.index("<tbody>", body.index("Recent Users")):]
    tbody = tbody[:tbody.index("</tbody>")]
    assert "<tr>" in tbody


def test_edit_plan_rejects_blank_price_with_clear_message_not_db_crash(app_module, db_session, client, admin, plan):
    """Live-reproduced bug: editing a plan (e.g. the seeded Free Trial plan,
    whose real price is a legitimate 0) without touching the Price field
    used to submit an empty string, which _plan_number_field treated as
    "unset" (correct for media caps, wrong for a NOT NULL price column) and
    crashed with a raw psycopg NotNullViolation - hidden behind a generic
    "Plan configuration was rejected" with no indication which field."""
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id
    resp = client.post(
        f"/admin/plans/{plan.id}/edit",
        data={"plan_name": plan.plan_name, "plan_amount": "", "total_project_limit": "9"},
    )
    assert resp.status_code in (302, 303)
    db_session.expire_all()
    refreshed = app_module.SubscriptionPlan.query.get(plan.id)
    assert refreshed.plan_amount is not None  # never actually reached NULL
    with client.session_transaction() as sess:
        flashes = sess.get("_flashes", [])
    assert any("Price is required" in msg for _cat, msg in flashes)


def test_edit_plan_price_field_shows_real_zero_not_blank(client, login_admin, plan):
    plan.plan_amount = 0.0
    from app import db as _db
    _db.session.commit()
    body = client.get(f"/admin/plans/{plan.id}/edit").data.decode()
    assert 'id="plan_amount"' in body
    snippet = body[body.index('id="plan_amount"'):body.index('id="plan_amount"') + 200]
    assert 'value="0.0"' in snippet or 'value="0"' in snippet


def test_repair_missing_trial_details_uses_trial_plan_not_system_config(app_module, db_session, normal_user):
    """The real fix: register() already read trial_plan.total_project_limit/
    total_scan_limit directly - this repair/rebuild path (used when a trial
    user's TrialDetails row is missing) used to read the free_trial_projects/
    free_trial_scans SystemConfig keys instead, a second, actually-dead
    source of truth the old Admin 'Trial Settings' page edited for no
    effect on real signups."""
    trial_plan = app_module.SubscriptionPlan.query.filter_by(is_trial_plan=True).first()
    trial_plan.total_project_limit = 41
    trial_plan.total_scan_limit = 42
    db_session.commit()
    app_module.set_system_config("free_trial_projects", 999, "integer", "x")
    app_module.set_system_config("free_trial_scans", 998, "integer", "x")

    normal_user.subscription_status = "trial"
    normal_user.subscription_taken_at = app_module.dt.utcnow()
    app_module.TrialDetails.query.filter_by(user_id=normal_user.id).delete()
    db_session.commit()
    trial, created = app_module._repair_missing_trial_details(normal_user, trial_plan)
    assert created is True
    assert trial.trial_project_limit == 41
    assert trial.trial_scan_limit == 42


def test_add_plan_currency_select_offers_inr_only(client, login_admin):
    body = client.get("/admin/plans/add").data.decode()
    assert 'value="INR"' in body
    assert 'value="USD"' not in body
    assert 'value="EUR"' not in body


def test_add_plan_and_edit_plan_banners_no_longer_claim_read_only(client, login_admin, plan):
    for path in ("/admin/plans/add", f"/admin/plans/{plan.id}/edit"):
        body = client.get(path).data.decode()
        assert "no admin form writes them yet" not in body


def test_edit_plan_lifecycle_select_shows_humanized_labels(client, login_admin, plan):
    body = client.get(f"/admin/plans/{plan.id}/edit").data.decode()
    assert ">Active<" in body or ">Active " in body
    # Raw enum text must not appear as visible option text (still fine as a
    # value attribute).
    assert ">ACTIVE<" not in body


def test_admin_bootstrap_alert_dark_theme_override_present():
    css = Path("static/css/admin-console.css").read_text(encoding="utf-8")
    assert ".ss-admin-scope .alert-info {" in css
    assert ".ss-admin-scope .alert-warning {" in css
    assert ".ss-admin-scope .alert-danger {" in css
    assert ".ss-admin-scope .alert-success {" in css


def test_admin_input_placeholder_contrast_rule_present():
    css = Path("static/css/admin-console.css").read_text(encoding="utf-8")
    assert ".ss-admin-scope input::placeholder," in css
    assert "rgba(230, 230, 233, 0.45)" in css


def test_payment_success_confirmation_note_does_not_overclaim_receipt():
    html = Path("templates/user/payment_success.html").read_text(encoding="utf-8")
    assert "We're emailing your receipt" not in html
    assert "We've emailed your payment confirmation" in html


# ---------------------------------------------------------------------------
# Narrow Admin UI Cleanup (2026-09-04) - Lane 4: Plan Duration UX
#
# models.py: `duration_type = db.Column(db.String(20), default='time')  #
# 'time' or 'count'` - there is no third "blank/Optional" state anywhere in
# the model. The Add/Edit Plan forms' `<option value="" selected>Select
# duration type (Optional)</option>` placeholder represented a state the
# backend never actually supported: submitting it always hit
# `_plan_form_values`'s `if duration_type not in PLAN_DURATION_TYPES` guard
# and failed. Live-reproduced via Playwright, root-caused by reading
# models.py directly, fixed by removing the fake placeholder (defaulting the
# select to "time", the model's own column default) rather than building
# conditional show/hide UI around a state that never worked.
# ---------------------------------------------------------------------------
def test_add_plan_duration_type_select_has_no_blank_placeholder(client, login_admin):
    body = client.get("/admin/plans/add").data.decode()
    idx = body.index('id="duration_type"')
    snippet = body[idx:idx + 400]
    assert 'value=""' not in snippet
    assert 'Optional' not in snippet
    assert 'value="time" selected' in snippet


def test_edit_plan_duration_type_select_has_no_blank_placeholder(client, login_admin, plan):
    body = client.get(f"/admin/plans/{plan.id}/edit").data.decode()
    idx = body.index('id="duration_type"')
    snippet = body[idx:idx + 400]
    assert 'value=""' not in snippet
    assert 'Optional' not in snippet


def test_add_plan_time_based_duration_saves_successfully(app_module, db_session, client, admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id
    resp = client.post(
        "/admin/plans/add",
        data={
            "plan_name": "QA Lane4 Time Plan",
            "plan_amount": "49",
            "duration_type": "time",
            "duration_value": "6",
            "max_pairs_per_project": "3",
        },
    )
    assert resp.status_code in (302, 303)
    created = app_module.SubscriptionPlan.query.filter_by(plan_name="QA Lane4 Time Plan").first()
    assert created is not None
    assert created.duration_type == "time"
    assert created.duration_value == 6


def test_add_plan_count_based_duration_saves_successfully(app_module, db_session, client, admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id
    resp = client.post(
        "/admin/plans/add",
        data={
            "plan_name": "QA Lane4 Count Plan",
            "plan_amount": "99",
            "duration_type": "count",
            "duration_value": "10",
            "max_pairs_per_project": "3",
        },
    )
    assert resp.status_code in (302, 303)
    created = app_module.SubscriptionPlan.query.filter_by(plan_name="QA Lane4 Count Plan").first()
    assert created is not None
    assert created.duration_type == "count"
    assert created.duration_value == 10


def test_edit_plan_rejects_tampered_duration_type_with_readable_message(app_module, db_session, client, admin, plan):
    """A manually-tampered/malformed duration_type (impossible via the real
    <select>, but a raw POST can send anything) must not surface the raw
    internal "Unsupported duration type." string - the form-level message
    must say what values are actually valid."""
    original_duration_type = plan.duration_type
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id
    resp = client.post(
        f"/admin/plans/{plan.id}/edit",
        data={"plan_name": plan.plan_name, "plan_amount": str(plan.plan_amount), "duration_type": "bogus_value"},
    )
    assert resp.status_code in (302, 303)
    db_session.expire_all()
    refreshed = app_module.SubscriptionPlan.query.get(plan.id)
    assert refreshed.duration_type == original_duration_type
    with client.session_transaction() as sess:
        flashes = sess.get("_flashes", [])
    messages = [msg for _cat, msg in flashes]
    assert any("Duration Type must be Time-based or Project-based" in msg for msg in messages)
    assert not any(msg.strip() == "Unsupported duration type." for msg in messages)
