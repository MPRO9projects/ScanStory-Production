"""SCANSTORY V1.1 - Human QA Blocker Fix Pass (2026-09-03).

Lane A: Admin mobile responsiveness (structural checks - CSS/markup, not
pixel rendering).
Lane B: Admin Create completion flow landed on Dashboard instead of the
Admin success/QR page - root cause was the shared resumable-upload JS
hardcoding User-only /success/<id> and /dashboard, never branching on
IS_ADMIN.
Lane C: Direct QR pre-play landing redesign (vertical balance), scoped to
before "Start story" only.

Run only this pack:
    python -m pytest tests/gate_jr/test_v11_qa_blocker_fixes.py -q
"""
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash


CREATE_TEMPLATE = Path("templates/user/user_create_project.html").read_text(encoding="utf-8")
SCANNER_TEMPLATE = Path("templates/user/scanner.html").read_text(encoding="utf-8")
PROJECTS_ADMIN_TEMPLATE = Path("templates/admin/projects.html").read_text(encoding="utf-8")
MANAGE_ADMINS_TEMPLATE = Path("templates/admin/manage_admins.html").read_text(encoding="utf-8")
ADMIN_CSS = Path("static/css/admin-console.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Lane B: Admin create completion flow
# ---------------------------------------------------------------------------

def test_create_project_js_no_longer_hardcodes_user_only_redirects():
    # The 4 real bug sites: hardcoded /success/<id> or /dashboard with no
    # IS_ADMIN branch at all.
    assert "window.location.href = `/success/${sessionPayload.session.project_id}`;" not in CREATE_TEMPLATE
    assert "window.location.href = finalSession.project_id ? `/success/${finalSession.project_id}` : '/dashboard';" not in CREATE_TEMPLATE
    assert "window.location.href = projectId ? `/success/${projectId}` : '/dashboard';" not in CREATE_TEMPLATE
    assert "window.location.href = xhr.responseURL || '/dashboard';" not in CREATE_TEMPLATE


def test_create_project_js_has_admin_aware_redirect_helpers():
    assert "function successUrlFor(projectId)" in CREATE_TEMPLATE
    assert "function postUploadFallbackUrl()" in CREATE_TEMPLATE
    assert "IS_ADMIN ? `/admin/success/${projectId}` : `/success/${projectId}`" in CREATE_TEMPLATE
    assert "IS_ADMIN ? '/admin/my-projects' : '/dashboard'" in CREATE_TEMPLATE
    # All 4 original call sites now route through the helpers.
    assert CREATE_TEMPLATE.count("successUrlFor(") >= 4  # 1 definition + >=3 call sites
    assert CREATE_TEMPLATE.count("postUploadFallbackUrl()") >= 4


def test_admin_success_page_renders_admin_context_not_dashboard(client, login_admin, app_module, db_session):
    project = app_module.Project(
        name="Tracked Overlay Success Check", owner_admin_id=login_admin.id,
        experience_type="image_video", playback_mode="tracked_overlay",
        qr_code_filename="project_1_admin.png", qr_code_path="/admin/qr/project_1_admin.png",
    )
    db_session.add(project)
    db_session.commit()
    pair = app_module.ProjectPair(
        project_id=project.id, pair_index=0,
        image_filename=f"{project.id}_0.jpg", video_filename=f"{project.id}_0.mp4",
        image_path=f"/admin/image/{project.id}/0",
        is_processed=True, processing_status="completed", feature_extraction_status="extracted",
    )
    db_session.add(pair)
    db_session.commit()

    resp = client.get(f"/admin/success/{project.id}")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Admin Dashboard" not in body
    assert f"/admin/projects/{project.id}/qr" in body
    assert f"/admin/project/{project.id}/scanner-test" in body
    assert "/admin/my-projects" in body
    assert f"/projects/{project.id}/edit" in body


def test_admin_success_page_works_for_detect_once(client, login_admin, app_module, db_session):
    project = app_module.Project(
        name="Detect Once Success Check", owner_admin_id=login_admin.id,
        experience_type="image_video", playback_mode="detect_once",
    )
    db_session.add(project)
    db_session.commit()
    resp = client.get(f"/admin/success/{project.id}")
    assert resp.status_code == 200
    assert b"Admin Dashboard" not in resp.data


def test_admin_success_page_works_for_direct_qr(client, login_admin, app_module, db_session):
    # The OLD legacy admin_handle_upload route is image_video-only, but the
    # modern resumable-upload API (what the real Admin Create UI actually
    # calls) already produces owner_admin_id-owned direct_qr projects
    # end-to-end - see test_admin_can_finalize_direct_qr_session_end_to_end
    # in tests/integration/test_resumable_upload.py. Built directly here
    # instead, to isolate this test to success-page rendering only.
    project = app_module.Project(
        name="Direct QR Success Check", owner_admin_id=login_admin.id,
        experience_type="direct_qr", playback_mode="direct",
    )
    db_session.add(project)
    db_session.commit()
    resp = client.get(f"/admin/success/{project.id}")
    assert resp.status_code == 200
    assert b"Admin Dashboard" not in resp.data


def test_admin_success_page_denies_other_admins_project(client, login_admin, app_module, db_session, admin):
    other = app_module.Admin(
        email="admin-qa-blocker-b@example.com", name="Other Admin",
        password_hash=generate_password_hash("OtherAdminPass123"), role="admin",
        is_active=True, created_by=admin.id,
    )
    db_session.add(other)
    db_session.commit()
    project = app_module.Project(name="Not Yours", owner_admin_id=other.id, experience_type="image_video")
    db_session.add(project)
    db_session.commit()

    # login_admin fixture already logs in as `admin`; the project belongs to `other`.
    resp = client.get(f"/admin/success/{project.id}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Lane C: Direct QR pre-play landing
# ---------------------------------------------------------------------------

def test_direct_qr_scanner_page_has_preplay_class(client, login_user, app_module, db_session, normal_user):
    project = app_module.Project(
        name="Preplay Landing Project", owner_user_id=normal_user.id,
        experience_type="direct_qr", playback_mode="direct",
    )
    db_session.add(project)
    db_session.commit()
    pair = app_module.ProjectPair(
        project_id=project.id, pair_index=0, image_filename=None, image_path=None,
        video_filename=f"{project.id}_0.mp4", is_processed=True,
        processing_status="completed", feature_extraction_status="not_required",
    )
    db_session.add(pair)
    db_session.commit()

    resp = client.get(f"/s/{project.public_key}")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'id="experienceIntro"' in body
    assert 'class="is-preplay"' in body
    assert "Ready when you are." not in body


def test_tracked_overlay_scanner_page_has_no_preplay_class(client, login_user, project_with_pair):
    project, _pair = project_with_pair
    resp = client.get(f"/s/{project.public_key}")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'id="experienceIntro" role="dialog"' in body
    # The .is-preplay CSS *rule* is always present (one shared inline
    # stylesheet for every mode) - only its DOM *application* differs. Check
    # the actual class attribute on the intro element, not a bare substring
    # search (which would also match the CSS selector text itself).
    intro_tag = body[body.index('<div id="experienceIntro"'):body.index('>', body.index('<div id="experienceIntro"')) + 1]
    assert "is-preplay" not in intro_tag


def test_start_direct_qr_playback_removes_preplay_class():
    idx = SCANNER_TEMPLATE.index("function startDirectQrPlayback()")
    body = SCANNER_TEMPLATE[idx:idx + 600]
    assert "experienceIntro.classList.remove('is-preplay')" in body


def test_preplay_css_scoped_and_removed_for_playback():
    # The pre-play rules must be scoped to .is-preplay so they stop applying
    # the instant the class is removed - not to a bare selector that would
    # also affect playback.
    assert 'body[data-experience-type="direct_qr"] #experienceIntro.is-preplay {' in SCANNER_TEMPLATE
    assert 'body[data-experience-type="direct_qr"] #experienceIntro.is-preplay::before' in SCANNER_TEMPLATE
    # Post-start selectors (#directQrVideo, playback nav) must not reference
    # .is-preplay at all - they stay unconditional/unaffected.
    assert '#directQrVideo.is-preplay' not in SCANNER_TEMPLATE
    assert '.dqr-controls.is-preplay' not in SCANNER_TEMPLATE


def test_direct_qr_post_start_dom_ids_unchanged():
    # Regression guard: none of the playback-side element ids were touched.
    for elem_id in ("directQrVideo", "directQrIndicator", "directQrControls",
                    "directQrNavNumbers", "directQrCompletion", "directQrReplayBtn",
                    "directQrChooserList", "directQrPlayBtn", "directQrPlaylistData"):
        assert f'id="{elem_id}"' in SCANNER_TEMPLATE, elem_id


# ---------------------------------------------------------------------------
# Lane A: Admin mobile responsiveness (structural)
# ---------------------------------------------------------------------------

def test_admin_action_buttons_no_longer_forced_into_single_column():
    assert "flex-direction: column;\n            }\n            \n            .table td {" not in PROJECTS_ADMIN_TEMPLATE
    # The specific broken rule is gone from both files.
    import re
    broken = re.compile(r"\.action-buttons\s*\{\s*flex-direction:\s*column;\s*\}")
    assert not broken.search(PROJECTS_ADMIN_TEMPLATE)
    assert not broken.search(MANAGE_ADMINS_TEMPLATE)


def test_admin_css_has_narrow_phone_breakpoint():
    assert "@media (max-width: 480px)" in ADMIN_CSS
    assert "flex: 1 1 calc(50% - 0.2rem);" in ADMIN_CSS


def test_admin_mobile_drawer_and_table_overflow_still_present():
    # Pre-existing hardening from earlier passes must still be intact.
    assert "@media (max-width: 767.98px)" in ADMIN_CSS
    assert ".ss-admin-scope .table-container" in ADMIN_CSS
    assert "#adminSidebar.sidebar" in ADMIN_CSS


def test_admin_my_projects_actions_wrapped_in_action_buttons_container(client, login_admin, app_module, db_session):
    project = app_module.Project(
        name="Mobile Action Grid Check", owner_admin_id=login_admin.id, experience_type="image_video",
    )
    db_session.add(project)
    db_session.commit()
    resp = client.get("/admin/my-projects")
    assert resp.status_code == 200
    body = resp.data.decode()
    idx = body.index(">Mobile Action Grid Check<")
    row = body[idx:body.index("</tr>", idx)]
    assert '<div class="action-buttons">' in row
