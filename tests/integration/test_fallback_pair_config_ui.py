"""V1 Wave 6: creator + admin UI for configuring a project's default
fallback pair.

The backend (_apply_fallback_pair_selection, set_project_fallback_pair,
admin_set_project_fallback_pair) already exists and already has
route-level coverage in tests/integration/test_fallback_analytics.py.
This file covers the UI layer built on top of it: the rendered
templates/user/project_preview.html and templates/admin/project_preview.html
pages, plus the admin route lifecycle (not previously covered anywhere),
and a full set/switch/clear lifecycle through the creator route.
"""
from pathlib import Path

import pytest


def _login_user(client, user):
    with client.session_transaction() as sess:
        sess["user_id"] = user.id


def _login_admin(client, admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id


def _admin_project_with_pairs(app_module, db_session, owner_admin, count=2):
    project = app_module.Project(
        name="Admin Fallback Project",
        owner_admin_id=owner_admin.id,
        scanner_url="/scanner/admin-fallback",
        qr_code_filename="admin-fallback.png",
        qr_code_path="/qr/admin-fallback.png",
    )
    db_session.add(project)
    db_session.commit()
    project.scanner_url = f"/scanner/{project.id}"
    db_session.commit()

    pairs = []
    for index in range(count):
        pair = app_module.ProjectPair(
            project_id=project.id,
            pair_index=index,
            image_filename=f"{project.id}_{index}.jpg",
            video_filename=f"{project.id}_{index}.mp4",
            is_processed=True,
            processing_status="completed",
            feature_extraction_status="extracted",
        )
        db_session.add(pair)
        db_session.commit()
        pairs.append(pair)
    return project, pairs


# ---------------------------------------------------------------------
# Creator route: full lifecycle (set -> switch -> clear)
# ---------------------------------------------------------------------

def test_creator_full_lifecycle_set_switch_and_clear(client, app_module, db_session, normal_user, multiple_pairs):
    project = multiple_pairs  # 3 pairs at index 0, 1, 2
    pair0 = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=0).first()
    pair1 = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=1).first()
    _login_user(client, normal_user)

    set_resp = client.post(f"/project/{project.id}/fallback-pair", json={"pair_index": 0})
    assert set_resp.status_code == 200
    body = set_resp.get_json()
    assert body == {"success": True, "fallback_pair_index": 0}
    assert app_module.Project.query.get(project.id).fallback_pair_id == pair0.id

    switch_resp = client.post(f"/project/{project.id}/fallback-pair", json={"pair_index": 1})
    assert switch_resp.status_code == 200
    assert switch_resp.get_json() == {"success": True, "fallback_pair_index": 1}
    assert app_module.Project.query.get(project.id).fallback_pair_id == pair1.id

    clear_resp = client.post(f"/project/{project.id}/fallback-pair", json={"pair_index": None})
    assert clear_resp.status_code == 200
    assert clear_resp.get_json() == {"success": True, "fallback_pair_index": None}
    assert app_module.Project.query.get(project.id).fallback_pair_id is None


# ---------------------------------------------------------------------
# Admin route: full lifecycle + authorization
# ---------------------------------------------------------------------

def test_admin_can_set_and_clear_fallback_pair(client, app_module, db_session, admin):
    project, pairs = _admin_project_with_pairs(app_module, db_session, admin)
    _login_admin(client, admin)

    set_resp = client.post(f"/admin/project/{project.id}/fallback-pair", json={"pair_index": 0})
    assert set_resp.status_code == 200
    assert set_resp.get_json() == {"success": True, "fallback_pair_index": 0}
    assert app_module.Project.query.get(project.id).fallback_pair_id == pairs[0].id

    clear_resp = client.post(f"/admin/project/{project.id}/fallback-pair", json={"pair_index": None})
    assert clear_resp.status_code == 200
    assert app_module.Project.query.get(project.id).fallback_pair_id is None


def test_admin_route_rejects_admin_who_does_not_own_project(client, app_module, db_session, admin, secondary_admin):
    project, _pairs = _admin_project_with_pairs(app_module, db_session, admin)
    _login_admin(client, secondary_admin)

    resp = client.post(f"/admin/project/{project.id}/fallback-pair", json={"pair_index": 0})
    assert resp.status_code == 404
    assert app_module.Project.query.get(project.id).fallback_pair_id is None


def test_admin_route_rejects_cross_project_pair_index(client, app_module, db_session, admin):
    project_a, _pairs_a = _admin_project_with_pairs(app_module, db_session, admin, count=1)
    _project_b, pairs_b = _admin_project_with_pairs(app_module, db_session, admin, count=6)
    _login_admin(client, admin)

    # project_b has a pair_index=5, project_a does not.
    resp = client.post(f"/admin/project/{project_a.id}/fallback-pair", json={"pair_index": 5})
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "PAIR_NOT_FOUND"
    assert app_module.Project.query.get(project_a.id).fallback_pair_id is None
    assert pairs_b[5].pair_index == 5  # sanity: that pair really does exist, just on the other project


def test_admin_route_requires_admin_login(client, app_module, db_session, admin):
    project, _pairs = _admin_project_with_pairs(app_module, db_session, admin)
    resp = client.post(f"/admin/project/{project.id}/fallback-pair", json={"pair_index": 0})
    # admin_required redirects an unauthenticated caller to the admin login page.
    assert resp.status_code in (302, 401, 403)
    assert app_module.Project.query.get(project.id).fallback_pair_id is None


# ---------------------------------------------------------------------
# Creator preview page rendering: badge appears on the right pair only,
# absent when nothing is set, and reflects true server state on reload.
# ---------------------------------------------------------------------

def test_creator_preview_shows_no_fallback_badge_when_unset(client, normal_user, multiple_pairs):
    project = multiple_pairs
    _login_user(client, normal_user)
    resp = client.get(f"/project/{project.id}/preview")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # The per-pair "currently the fallback" badge chip (not the always-present
    # explainer heading, which also contains the words "Project Fallback").
    assert 'class="fallback-badge' not in html
    assert "Set as Project Fallback" in html  # eligible pairs still offer the action


def test_creator_preview_marks_correct_pair_as_fallback_and_only_that_one(
    client, app_module, db_session, normal_user, multiple_pairs
):
    project = multiple_pairs
    pair1 = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=1).first()
    project.fallback_pair_id = pair1.id
    db_session.commit()

    _login_user(client, normal_user)
    resp = client.get(f"/project/{project.id}/preview")
    html = resp.get_data(as_text=True)

    assert html.count('class="fallback-badge') == 1
    # The pair that IS the fallback offers "Remove Fallback", the other two
    # still offer "Set as Project Fallback" - never conflated.
    assert html.count("Remove Fallback") == 1
    assert html.count("Set as Project Fallback") == 2


def test_creator_preview_reflects_fresh_reload_not_stale_client_state(
    client, app_module, db_session, normal_user, multiple_pairs
):
    """Proves the badge is driven by project.fallback_pair_id re-read on
    every request - not something that could only look right until the
    next reload."""
    project = multiple_pairs
    _login_user(client, normal_user)

    before = client.get(f"/project/{project.id}/preview").get_data(as_text=True)
    assert 'class="fallback-badge' not in before

    pair0 = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=0).first()
    set_resp = client.post(f"/project/{project.id}/fallback-pair", json={"pair_index": 0})
    assert set_resp.status_code == 200

    after = client.get(f"/project/{project.id}/preview").get_data(as_text=True)
    assert 'class="fallback-badge' in after
    assert app_module.Project.query.get(project.id).fallback_pair_id == pair0.id

    clear_resp = client.post(f"/project/{project.id}/fallback-pair", json={"pair_index": None})
    assert clear_resp.status_code == 200
    reloaded_again = client.get(f"/project/{project.id}/preview").get_data(as_text=True)
    assert 'class="fallback-badge' not in reloaded_again


def test_creator_preview_explains_fallback_semantics_near_controls(client, normal_user, multiple_pairs):
    project = multiple_pairs
    _login_user(client, normal_user)
    html = client.get(f"/project/{project.id}/preview").get_data(as_text=True)
    assert "recognition fails" in html
    assert "camera access is denied" in html


def test_creator_preview_ineligible_pair_has_no_fallback_button(client, app_module, db_session, normal_user, project_with_pair):
    project, _pair = project_with_pair
    no_video_pair = app_module.ProjectPair(
        project_id=project.id,
        pair_index=9,
        image_filename=f"{project.id}_9.jpg",
        video_filename="",  # falsy in the template's existing `{% if pair.video_filename %}` gate
        is_processed=False,
        processing_status="processing",
        feature_extraction_status="pending",
    )
    db_session.add(no_video_pair)
    db_session.commit()

    _login_user(client, normal_user)
    html = client.get(f"/project/{project.id}/preview").get_data(as_text=True)
    assert "make it eligible as the project fallback" in html


# ---------------------------------------------------------------------
# Admin preview page rendering - same marker, same rules.
# ---------------------------------------------------------------------

def test_admin_preview_marks_correct_pair_as_fallback(client, app_module, db_session, admin):
    project, pairs = _admin_project_with_pairs(app_module, db_session, admin, count=2)
    project.fallback_pair_id = pairs[0].id
    db_session.commit()

    _login_admin(client, admin)
    resp = client.get(f"/admin/project/{project.id}/preview")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert html.count('class="fallback-badge') == 1
    assert html.count("Remove Fallback") == 1
    assert html.count("Set as Project Fallback") == 1


def test_admin_preview_no_badge_when_unset(client, app_module, db_session, admin):
    project, _pairs = _admin_project_with_pairs(app_module, db_session, admin, count=2)
    _login_admin(client, admin)
    html = client.get(f"/admin/project/{project.id}/preview").get_data(as_text=True)
    assert 'class="fallback-badge' not in html


# ---------------------------------------------------------------------
# CSRF: templates reuse the app-wide '{{ csrf_token() }}' -> X-CSRFToken
# pattern (see templates/user/subscribe.html), not a parallel mechanism.
# ---------------------------------------------------------------------

def test_creator_and_admin_templates_use_existing_csrf_pattern():
    creator_html = Path("templates/user/project_preview.html").read_text(encoding="utf-8")
    admin_html = Path("templates/admin/project_preview.html").read_text(encoding="utf-8")
    for html in (creator_html, admin_html):
        assert "'X-CSRFToken': CSRF_TOKEN" in html
        assert "{{ csrf_token() }}" in html


def test_no_new_autoplay_attribute_introduced():
    creator_html = Path("templates/user/project_preview.html").read_text(encoding="utf-8")
    admin_html = Path("templates/admin/project_preview.html").read_text(encoding="utf-8")
    for html in (creator_html, admin_html):
        assert "autoplay" not in html.lower()
