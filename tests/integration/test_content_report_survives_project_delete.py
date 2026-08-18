"""ContentReport survives a project hard-delete (V1.1 production-ops).

THE DEFECT
`ContentReport.project_id` was NOT NULL and the ORM relationship carried
`cascade="all, delete-orphan"`, so the superadmin hard-delete path destroyed
every moderation report filed against a project along with the project. The
record of WHY content was removed disappeared with the content.

THE FIX, and what this file pins
project_id is nullable with ON DELETE SET NULL and the delete-orphan cascade is
gone, so a hard-deleted project DETACHES its reports. This suite proves the
report row and every field on it survive, that Admin still renders and can still
act on a detached report, that it never links to /admin/projects/None, and that
"suspend project" is refused rather than silently recorded against a project that
no longer exists.

FOREIGN-KEY CAVEAT. The integration suite builds its schema with
`db.create_all()` on SQLite, which does not enforce foreign keys without
`PRAGMA foreign_keys=ON`. What detaches the report here is therefore SQLAlchemy's
default relationship behaviour (de-associate on parent delete) rather than the
database constraint. The database's own ON DELETE SET NULL - the mechanism that
also covers raw DELETEs and paths that never touch the ORM - is proven separately
against an FK-enforcing engine in
tests/migrations/test_content_report_delete_migration.py. Both layers agree, and
both are needed: the DB rule for coverage, the ORM default so no code path has to
remember.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _make_user(app_module, db_session, email):
    user = app_module.User(
        email=email,
        first_name=email.split("@")[0],
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_status="active",
        subscription_expires_at=datetime.utcnow() + timedelta(days=30),
        subscribed_project_limit=3,
        subscribed_scan_limit=100,
        projects_used=1,
        scans_used=0,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _make_project(app_module, db_session, owner, *, name="Reported Project", index=1):
    project = app_module.Project(
        name=name,
        owner_user_id=owner.id,
        created_by_user_id=owner.id,
        current_owner_user_id=owner.id,
        user_project_index=index,
        scanner_url="/scanner/reported",
        qr_code_filename=f"project_reported_{index}.png",
        qr_code_path=f"/qr/project_reported_{index}.png",
        is_active=True,
    )
    db_session.add(project)
    db_session.commit()

    image_path = Path(app_module.IMAGES_DIR) / f"{project.id}_0.jpg"
    video_path = Path(app_module.VIDEOS_DIR) / f"{project.id}_0.mp4"
    for path in (image_path, video_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"marker image bytes")
    video_path.write_bytes(b"overlay video bytes")

    pair = app_module.ProjectPair(
        project_id=project.id,
        pair_index=0,
        image_filename=image_path.name,
        video_filename=video_path.name,
    )
    db_session.add(pair)
    db_session.commit()
    return project, (image_path, video_path)


def _make_report(app_module, db_session, project, **kwargs):
    report = app_module.ContentReport(
        project_id=project.id,
        reason=kwargs.pop("reason", "COPYRIGHT_OR_IP"),
        status=kwargs.pop("status", "OPEN"),
        details=kwargs.pop("details", "Uses my footage without permission."),
        reporter_session_hash=kwargs.pop("reporter_session_hash", "session-hash-abc"),
        reporter_ip_hash=kwargs.pop("reporter_ip_hash", "ip-hash-def"),
        **kwargs,
    )
    db_session.add(report)
    db_session.commit()
    return report


def _login_admin(client, admin_obj):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin_obj.id


def _hard_delete(client, project):
    response = client.post(f"/admin/projects/{project.id}/delete")
    assert response.status_code in (200, 302), response.status_code
    return response


@pytest.fixture()
def reported(client, app_module, db_session, admin):
    """A live project with a fully populated, already-reviewed report on it."""
    owner = _make_user(app_module, db_session, "reported-owner@example.com")
    project, media = _make_project(app_module, db_session, owner)
    report = _make_report(
        app_module,
        db_session,
        project,
        status="ACTION_TAKEN",
        reporter_email="rights-holder@example.com",
        resolution_action="CREATOR_CONTACT_REQUIRED",
        resolution_reason="Asked the creator to remove the clip.",
        reviewed_by_admin_id=admin.id,
        reviewed_at=datetime.utcnow(),
    )
    _login_admin(client, admin)
    return {"owner": owner, "project": project, "report": report, "media": media}


def _detail(client, report_id):
    response = client.get(f"/admin/reports/{report_id}")
    assert response.status_code == 200, response.status_code
    return response.get_json()["report"]


# ---------------------------------------------------------------------------
# 1-2. the report detail endpoint before and after the project is destroyed
# ---------------------------------------------------------------------------
def test_report_detail_renders_while_the_project_still_exists(client, reported):
    payload = _detail(client, reported["report"].id)
    assert payload["project_id"] == reported["project"].id
    assert payload["project_name"] == "Reported Project"
    assert payload["project_deleted"] is False
    assert payload["project_is_active"] is True


def test_report_detail_still_renders_after_the_project_is_hard_deleted(client, reported):
    _hard_delete(client, reported["project"])

    payload = _detail(client, reported["report"].id)
    assert payload["project_id"] is None
    assert payload["project_name"] is None
    assert payload["project_deleted"] is True
    # Safe semantics, not invented ones: nothing is claimed about a project
    # that no longer exists.
    assert payload["project_is_active"] is None
    assert payload["project_is_publicly_live"] is None
    assert payload["project_owner_type"] is None
    assert payload["project_owner_user_id"] is None


# ---------------------------------------------------------------------------
# 3-8. every field on the report survives the delete
# ---------------------------------------------------------------------------
def test_detached_report_still_names_the_reporter_that_was_recorded(client, app_module, db_session, admin, reported):
    reporter = _make_user(app_module, db_session, "identified-reporter@example.com")
    reported["report"].reporter_user_id = reporter.id
    db_session.commit()

    _hard_delete(client, reported["project"])

    payload = _detail(client, reported["report"].id)
    assert payload["reporter_user_id"] == reporter.id
    assert payload["has_reporter_contact"] is True, "a deleted project must not anonymise the reporter"


def test_detached_report_that_was_anonymous_stays_anonymous(client, app_module, db_session, admin):
    owner = _make_user(app_module, db_session, "anon-owner@example.com")
    project, _media = _make_project(app_module, db_session, owner, name="Anon Reported", index=2)
    report = _make_report(app_module, db_session, project, reason="SPAM")
    _login_admin(client, admin)

    _hard_delete(client, project)

    payload = _detail(client, report.id)
    assert payload["project_deleted"] is True
    assert payload["reporter_user_id"] is None
    assert payload["has_reporter_contact"] is False


def test_detached_report_preserves_its_reason(client, reported):
    _hard_delete(client, reported["project"])
    assert _detail(client, reported["report"].id)["reason"] == "COPYRIGHT_OR_IP"


def test_detached_report_preserves_its_details(client, reported):
    _hard_delete(client, reported["project"])
    assert _detail(client, reported["report"].id)["details"] == "Uses my footage without permission."


def test_detached_report_preserves_its_status(client, reported):
    _hard_delete(client, reported["project"])
    assert _detail(client, reported["report"].id)["status"] == "ACTION_TAKEN"


def test_detached_report_preserves_its_review_and_resolution_metadata(client, admin, reported):
    _hard_delete(client, reported["project"])

    payload = _detail(client, reported["report"].id)
    assert payload["resolution_action"] == "CREATOR_CONTACT_REQUIRED"
    assert payload["resolution_reason"] == "Asked the creator to remove the clip."
    assert payload["reviewed_by_admin_id"] == admin.id
    assert payload["reviewed_at"] is not None
    assert payload["created_at"] is not None


# ---------------------------------------------------------------------------
# 9-11. what Admin actually renders for a detached report
# ---------------------------------------------------------------------------
def test_no_project_link_is_offered_for_a_detached_report(client, reported):
    _hard_delete(client, reported["project"])
    payload = _detail(client, reported["report"].id)
    # There is no id to link to, and the flag the page keys off is set.
    assert payload["project_id"] is None
    assert payload["project_deleted"] is True


def test_moderation_page_renders_deleted_project_state_explicitly(client, reported):
    _hard_delete(client, reported["project"])

    page = client.get("/admin/moderation")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "project_deleted" in body, "the page must branch on the detached state"
    assert "Deleted - no longer available" in body
    assert "ScanStory deleted" in body


def test_moderation_page_never_builds_a_project_url_from_a_missing_id(client, reported):
    _hard_delete(client, reported["project"])
    body = client.get("/admin/moderation").get_data(as_text=True)
    for broken in ("/admin/projects/None", "/admin/projects/null", "/admin/projects/undefined"):
        assert broken not in body, f"detached report must not link to {broken}"


def test_report_queue_still_lists_the_detached_report(client, app_module, db_session, admin, reported):
    other_owner = _make_user(app_module, db_session, "other-owner@example.com")
    live_project, _media = _make_project(app_module, db_session, other_owner, name="Live Project", index=3)
    live_report = _make_report(app_module, db_session, live_project, reason="HATE_OR_HARASSMENT")

    _hard_delete(client, reported["project"])

    listing = client.get("/admin/reports")
    assert listing.status_code == 200
    reports = {r["id"]: r for r in listing.get_json()["reports"]}
    assert reported["report"].id in reports, "the queue must not lose detached history"
    assert reports[reported["report"].id]["project_deleted"] is True
    # The unrelated report is untouched by someone else's delete.
    assert reports[live_report.id]["project_deleted"] is False
    assert reports[live_report.id]["project_id"] == live_project.id


# ---------------------------------------------------------------------------
# 12-14. moderation actions against a detached report
# ---------------------------------------------------------------------------
def test_a_detached_report_can_still_be_dismissed_and_resolved(client, app_module, db_session, admin, reported):
    _hard_delete(client, reported["project"])

    response = client.post(
        f"/admin/reports/{reported['report'].id}/review",
        json={"status": "DISMISSED", "resolution_reason": "Content already gone; closing."},
    )
    assert response.status_code == 200
    payload = response.get_json()["report"]
    assert payload["status"] == "DISMISSED"
    assert payload["resolution_reason"] == "Content already gone; closing."
    assert payload["project_deleted"] is True
    assert app_module.ContentReport.query.get(reported["report"].id).status == "DISMISSED"


def test_suspending_a_deleted_project_is_refused_instead_of_falsely_succeeding(client, app_module, db_session, admin, reported):
    _hard_delete(client, reported["project"])
    before = app_module.ContentReport.query.get(reported["report"].id).status

    response = client.post(
        f"/admin/reports/{reported['report'].id}/review",
        json={"status": "ACTION_TAKEN", "resolution_action": "PROJECT_SUSPENDED"},
    )

    # A business error - not a 500, and not a silent success that would record a
    # suspension that never happened.
    assert response.status_code == 409
    body = response.get_json()
    assert body["success"] is False
    assert body["code"] == "PROJECT_UNAVAILABLE"
    report = app_module.ContentReport.query.get(reported["report"].id)
    assert report.status == before, "a refused action must not mutate the report"
    assert report.resolution_action == "CREATOR_CONTACT_REQUIRED", "prior decision preserved"


def test_suspending_a_live_reported_project_still_works(client, app_module, db_session, admin):
    """The existing, correct path stays green - the guard is narrow."""
    owner = _make_user(app_module, db_session, "live-owner@example.com")
    project, _media = _make_project(app_module, db_session, owner, name="Live Suspendable", index=4)
    report = _make_report(app_module, db_session, project, reason="HATE_OR_HARASSMENT")
    _login_admin(client, admin)

    response = client.post(
        f"/admin/reports/{report.id}/review",
        json={"status": "ACTION_TAKEN", "resolution_action": "PROJECT_SUSPENDED",
              "resolution_reason": "Policy breach confirmed."},
    )
    assert response.status_code == 200
    db_session.refresh(project)
    assert project.is_active is False
    assert app_module.ContentReport.query.get(report.id).resolution_action == "PROJECT_SUSPENDED"


# ---------------------------------------------------------------------------
# 15-18. the hard-delete path itself
# ---------------------------------------------------------------------------
def test_hard_delete_does_not_delete_the_reports_filed_against_the_project(client, app_module, db_session, admin, reported):
    report_id = reported["report"].id
    assert app_module.ContentReport.query.count() == 1

    _hard_delete(client, reported["project"])

    assert app_module.ContentReport.query.count() == 1, "moderation history was destroyed"
    assert app_module.ContentReport.query.get(report_id) is not None


def test_hard_delete_still_removes_the_project_and_its_media(client, app_module, db_session, admin, reported):
    """Deletion semantics themselves are unchanged - only report retention moved."""
    project_id = reported["project"].id
    image_path, video_path = reported["media"]
    assert image_path.exists() and video_path.exists()

    _hard_delete(client, reported["project"])

    assert app_module.Project.query.get(project_id) is None
    assert app_module.ProjectPair.query.filter_by(project_id=project_id).count() == 0
    assert not image_path.exists()
    assert not video_path.exists()


def test_hard_delete_leaves_reports_on_other_projects_completely_alone(client, app_module, db_session, admin, reported):
    other_owner = _make_user(app_module, db_session, "untouched-owner@example.com")
    other_project, _media = _make_project(app_module, db_session, other_owner, name="Untouched", index=5)
    other_report = _make_report(app_module, db_session, other_project, reason="SPAM", status="UNDER_REVIEW")

    _hard_delete(client, reported["project"])

    surviving = app_module.ContentReport.query.get(other_report.id)
    assert surviving is not None
    assert surviving.project_id == other_project.id
    assert surviving.status == "UNDER_REVIEW"
    assert app_module.Project.query.get(other_project.id) is not None


def test_hard_delete_records_its_own_admin_activity_unchanged(client, app_module, db_session, admin, reported):
    """The existing audit entry is preserved - no second mechanism was invented."""
    project_id = reported["project"].id
    _hard_delete(client, reported["project"])

    entries = app_module.AdminActivity.query.filter_by(activity_type="project_delete").all()
    assert len(entries) == 1
    assert f"ID: {project_id}" in entries[0].description


# ---------------------------------------------------------------------------
# 19-21. the surrounding contract is unchanged
# ---------------------------------------------------------------------------
def test_reporting_a_live_project_end_to_end_remains_green(client, app_module, db_session, admin):
    owner = _make_user(app_module, db_session, "flow-owner@example.com")
    project, _media = _make_project(app_module, db_session, owner, name="Flow Project", index=6)

    response = client.post(
        f"/api/projects/{project.id}/report",
        json={"reason": "SPAM", "details": "Unsolicited advertising."},
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    report = app_module.ContentReport.query.filter_by(project_id=project.id).one()
    assert report.status == "OPEN"
    assert report.reason == "SPAM"


def test_permission_gates_on_detached_reports_are_unchanged(client, app_module, db_session, admin, secondary_admin, monkeypatch, reported):
    _hard_delete(client, reported["project"])
    report_id = reported["report"].id

    # Unauthenticated cannot read or mutate a detached report either.
    fresh = app_module.app.test_client()
    assert fresh.get(f"/admin/reports/{report_id}").status_code in (301, 302)
    assert fresh.post(f"/admin/reports/{report_id}/review", json={"status": "DISMISSED"}).status_code in (301, 302)

    monkeypatch.setitem(
        app_module.ADMIN_ROLE_PERMISSIONS, "admin",
        app_module.ADMIN_ROLE_PERMISSIONS["admin"] - {"admin.reports.manage"},
    )
    limited = app_module.app.test_client()
    _login_admin(limited, secondary_admin)
    denied = limited.post(f"/admin/reports/{report_id}/review", json={"status": "DISMISSED"})
    assert denied.status_code in (301, 302, 403)
    assert app_module.ContentReport.query.get(report_id).status == "ACTION_TAKEN"


def test_no_route_deletes_a_content_report(app_module):
    """Retention is the whole point: nothing may introduce report deletion."""
    source = Path(app_module.__file__).read_text(encoding="utf-8", errors="replace")
    for forbidden in (
        "delete(report)",
        "ContentReport.query.delete",
        "ContentReport).delete",
        "delete(ContentReport",
    ):
        assert forbidden not in source, f"report deletion introduced via {forbidden!r}"
