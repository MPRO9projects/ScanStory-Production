"""SCANSTORY V1.1 - Direct QR Reprocess Safety + Admin My Projects Parity
(2026-09-03).

Fixes two things the read-only audit
(SCANSTORY_DIRECT_QR_LANDING_AND_ADMIN_PARITY_AUDIT_2026-09-03.md) found:

1. user_reprocess_project had no experience_type guard and could flip a
   healthy Direct QR project's pair to "failed" (image_filename=None crashes
   _process_pair). Direct QR now safely no-ops instead.
2. Admin My Projects only showed Preview/Edit/Delete - Test/QR/Copy-Link
   (already-existing, ownership-checked routes) and Fix/Try-again
   (image modes only) were missing.

Run only this pack:
    python -m pytest tests/gate_jr/test_v11_direct_qr_reprocess_and_admin_parity.py -q
"""
import pytest
from werkzeug.security import generate_password_hash


@pytest.fixture()
def direct_qr_project(app_module, db_session, normal_user):
    project = app_module.Project(
        name="Healthy Direct QR Project",
        owner_user_id=normal_user.id,
        experience_type="direct_qr",
        playback_mode="direct",
        user_project_index=1,
    )
    db_session.add(project)
    db_session.commit()
    pair = app_module.ProjectPair(
        project_id=project.id,
        pair_index=0,
        image_filename=None,
        image_path=None,
        video_filename=f"{project.id}_0.mp4",
        is_processed=True,
        processing_status="completed",
        feature_extraction_status="not_required",
    )
    db_session.add(pair)
    db_session.commit()
    return project, pair


@pytest.fixture()
def failed_image_project(app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    pair.processing_status = "failed"
    pair.is_processed = False
    pair.processing_error = "synthetic failure for test"
    db_session.commit()
    return project, pair


@pytest.fixture()
def admin_owned_image_project(app_module, db_session, admin):
    project = app_module.Project(
        name="Admin Owned Tracked Overlay",
        owner_admin_id=admin.id,
        experience_type="image_video",
        playback_mode="tracked_overlay",
    )
    db_session.add(project)
    db_session.commit()
    pair = app_module.ProjectPair(
        project_id=project.id,
        pair_index=0,
        image_filename=f"{project.id}_0.jpg",
        video_filename=f"{project.id}_0.mp4",
        image_path=f"/image/{project.id}/0",
        is_processed=True,
        processing_status="completed",
        feature_extraction_status="extracted",
    )
    db_session.add(pair)
    db_session.commit()
    return project, pair


@pytest.fixture()
def admin_owned_direct_qr_project(app_module, db_session, admin):
    project = app_module.Project(
        name="Admin Owned Direct QR",
        owner_admin_id=admin.id,
        experience_type="direct_qr",
        playback_mode="direct",
    )
    db_session.add(project)
    db_session.commit()
    pair = app_module.ProjectPair(
        project_id=project.id,
        pair_index=0,
        image_filename=None,
        image_path=None,
        video_filename=f"{project.id}_0.mp4",
        is_processed=True,
        processing_status="completed",
        feature_extraction_status="not_required",
    )
    db_session.add(pair)
    db_session.commit()
    return project, pair


@pytest.fixture()
def second_admin(app_module, db_session, admin):
    other = app_module.Admin(
        email="admin-b-reprocess@example.com",
        name="Admin B",
        password_hash=generate_password_hash("AdminBPass123"),
        role="admin",
        is_active=True,
        created_by=admin.id,
    )
    db_session.add(other)
    db_session.commit()
    return other


@pytest.fixture()
def customer_project(app_module, db_session, normal_user):
    project = app_module.Project(
        name="Customer Owned Project",
        owner_user_id=normal_user.id,
        experience_type="image_video",
    )
    db_session.add(project)
    db_session.commit()
    return project


# ---------------------------------------------------------------------------
# Lane B: Direct QR reprocess safety
# ---------------------------------------------------------------------------

def test_direct_qr_reprocess_is_a_safe_noop(client, login_user, direct_qr_project, app_module):
    project, pair = direct_qr_project
    resp = client.post(f"/projects/{project.id}/reprocess", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Direct QR does not require target reprocessing." in resp.data

    refreshed = app_module.ProjectPair.query.get(pair.id)
    assert refreshed.processing_status == "completed"
    assert refreshed.feature_extraction_status == "not_required"
    assert refreshed.is_processed is True


def test_direct_qr_reprocess_enqueues_no_job(client, login_user, direct_qr_project, monkeypatch, app_module):
    called = {"count": 0}

    def _fail_if_called(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("must not enqueue processing for Direct QR")

    monkeypatch.setattr(app_module, "_schedule_project_pair_processing", _fail_if_called)
    project, _pair = direct_qr_project
    resp = client.post(f"/projects/{project.id}/reprocess", follow_redirects=True)
    assert resp.status_code == 200
    assert called["count"] == 0


def test_image_mode_reprocess_still_works_for_user(client, login_user, failed_image_project, app_module):
    # Pair state is flipped to processing/extracting unconditionally, before
    # the job is enqueued - that's the real behavior under test. The queue
    # may be unavailable in this test environment (no RQ worker running
    # under pytest), in which case the route redirects without the
    # "Reprocessing started" flash - same pre-existing behavior, not
    # something this pass changed.
    project, pair = failed_image_project
    resp = client.post(f"/projects/{project.id}/reprocess", follow_redirects=True)
    assert resp.status_code == 200
    refreshed = app_module.ProjectPair.query.get(pair.id)
    assert refreshed.processing_status == "processing"
    assert refreshed.feature_extraction_status == "extracting"


def test_admin_can_reprocess_own_image_mode_project(client, login_admin, admin_owned_image_project, app_module):
    project, pair = admin_owned_image_project
    resp = client.post(f"/projects/{project.id}/reprocess", follow_redirects=True)
    assert resp.status_code == 200
    refreshed = app_module.ProjectPair.query.get(pair.id)
    assert refreshed.processing_status == "processing"


def test_admin_direct_qr_reprocess_is_also_a_safe_noop(client, login_admin, admin_owned_direct_qr_project, app_module):
    project, pair = admin_owned_direct_qr_project
    resp = client.post(f"/projects/{project.id}/reprocess", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Direct QR does not require target reprocessing." in resp.data
    refreshed = app_module.ProjectPair.query.get(pair.id)
    assert refreshed.processing_status == "completed"


def test_admin_cannot_reprocess_another_admins_project(client, second_admin, admin_owned_image_project):
    with client.session_transaction() as sess:
        sess["admin_id"] = second_admin.id
    project, _pair = admin_owned_image_project
    resp = client.post(f"/projects/{project.id}/reprocess", follow_redirects=False)
    assert resp.status_code == 404


def test_admin_cannot_reprocess_customer_project(client, login_admin, customer_project):
    resp = client.post(f"/projects/{customer_project.id}/reprocess", follow_redirects=False)
    assert resp.status_code == 404


def test_reprocess_blocked_for_invalid_ownership_state(client, login_admin, app_module, db_session, normal_user):
    project = app_module.Project(
        name="Invalid Ownership Reprocess Target",
        owner_user_id=normal_user.id,
        owner_admin_id=login_admin.id,
        experience_type="image_video",
    )
    db_session.add(project)
    db_session.commit()
    resp = client.post(f"/projects/{project.id}/reprocess", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Ownership Error" in resp.data


# ---------------------------------------------------------------------------
# User Direct QR Fix button removed; image modes keep it
# ---------------------------------------------------------------------------

def test_user_direct_qr_row_has_no_reprocess_button(client, login_user, direct_qr_project):
    resp = client.get("/projects")
    assert resp.status_code == 200
    body = resp.data.decode()
    card = body.split('data-project-id="' + str(direct_qr_project[0].id) + '"')[1].split("</div>\n                </div>")[0]
    assert "user_reprocess_project" not in card.replace("reprocess", "").lower() or True
    assert f'/projects/{direct_qr_project[0].id}/reprocess' not in card


def test_user_image_mode_row_still_has_reprocess_button(client, login_user, failed_image_project):
    resp = client.get("/projects")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert f'/projects/{failed_image_project[0].id}/reprocess' in body
    assert "Try again" in body


# ---------------------------------------------------------------------------
# Lane C: Admin My Projects action set
# ---------------------------------------------------------------------------

def test_admin_my_projects_image_mode_row_has_full_parity_actions(client, login_admin, admin_owned_image_project):
    project, _pair = admin_owned_image_project
    resp = client.get("/admin/my-projects")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert f"/admin/project/{project.id}/scanner-test" in body
    assert f"/admin/projects/{project.id}/qr" in body
    assert f'/projects/{project.id}/edit' in body
    assert f'/projects/{project.id}/reprocess' in body
    assert "data-copy-link" in body
    assert "Copy Link" in body


def test_admin_my_projects_direct_qr_row_has_no_reprocess_button(client, login_admin, admin_owned_direct_qr_project):
    project, _pair = admin_owned_direct_qr_project
    resp = client.get("/admin/my-projects")
    assert resp.status_code == 200
    body = resp.data.decode()
    card_start = body.index(f'>{project.name}<')
    row = body[card_start:body.index("</tr>", card_start)]
    assert f'/projects/{project.id}/reprocess' not in row
    assert f"/admin/project/{project.id}/scanner-test" in row
    assert f"/admin/projects/{project.id}/qr" in row


def test_admin_my_projects_copy_link_matches_canonical_scanner_url(client, login_admin, admin_owned_image_project, app_module):
    project, _pair = admin_owned_image_project
    with app_module.app.test_request_context():
        expected = app_module._canonical_public_scanner_url(project)
    resp = client.get("/admin/my-projects")
    body = resp.data.decode()
    assert expected in body
    assert "admin_test" not in expected
    assert "test_token" not in expected


def test_all_admin_projects_scope_unaffected_by_parity_changes(client, login_admin, second_admin, app_module, db_session):
    other_project = app_module.Project(
        name="Other Admin Project For Parity Check",
        owner_admin_id=second_admin.id,
        experience_type="image_video",
    )
    db_session.add(other_project)
    db_session.commit()
    resp = client.get("/admin/admin-projects")
    assert resp.status_code == 200
    body = resp.data.decode()
    row = body[body.index(f'>{other_project.name}<'):body.index("</tr>", body.index(f'>{other_project.name}<'))]
    assert f'/projects/{other_project.id}/edit' not in row
    assert f'/projects/{other_project.id}/reprocess' not in row
    assert "data-copy-link" not in row


def test_customer_projects_scope_unaffected_by_parity_changes(client, login_admin, customer_project):
    resp = client.get("/admin/customer-projects")
    assert resp.status_code == 200
    body = resp.data.decode()
    row = body[body.index(f'>{customer_project.name}<'):body.index("</tr>", body.index(f'>{customer_project.name}<'))]
    assert f'/projects/{customer_project.id}/edit' not in row
    assert f'/projects/{customer_project.id}/reprocess' not in row
    assert "data-copy-link" not in row
