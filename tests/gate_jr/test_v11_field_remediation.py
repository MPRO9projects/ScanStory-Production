"""V1.1 field-QA remediation pass: focused tests for the fixes made after the
first real physical-device certification round.

Covers: QA/dev-test account usability (reusing existing DEV_TEST_USER
infrastructure, not a new mechanism), the multi-video project-preview view,
the iPhone Safari login zoom-on-focus regression, and the support-email
replacement. Fast Video Phase 2 backend behavior is unchanged by this pass -
no new Fast Video tests are added here.
"""
from pathlib import Path

import pytest


# ===========================================================================
# 1. QA / dev-test account: reuse of existing infrastructure, not a new one
# ===========================================================================
def test_dev_test_account_gets_unlimited_entitlements(app_module, db_session, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("SCANSTORY_DEV_TESTING", "1")
    monkeypatch.delenv("SCANSTORY_PRODUCTION", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ENV", raising=False)

    result = app_module._seed_dev_test_users()
    assert result["created"] + result["updated"] + result["skipped"] == len(app_module.DEV_TEST_USER_EMAILS)

    user = app_module.User.query.filter_by(email="scanstorytest01@gmail.com").first()
    assert user is not None
    assert user.is_verified is True
    assert app_module.has_dev_test_entitlement(user) is True
    assert app_module.get_plan_pairs_limit(user) is None  # unlimited

    ent = app_module.user_entitlements(user)
    assert ent["effective_project_limit"] is None
    assert ent["effective_scan_limit"] is None
    assert ent["allow_multi_video_per_target"] is True
    assert ent["unlimited"] is True


def test_dev_test_account_not_entitled_without_flag(app_module, db_session, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.delenv("SCANSTORY_DEV_TESTING", raising=False)

    with pytest.raises(Exception):
        app_module._seed_dev_test_users()


def test_dev_test_seed_command_refuses_when_production_flag_active(app_module, db_session, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("SCANSTORY_DEV_TESTING", "1")
    monkeypatch.setenv("SCANSTORY_PRODUCTION", "1")

    with pytest.raises(Exception):
        app_module._seed_dev_test_users()


# ===========================================================================
# 2. Multi-video project preview
# ===========================================================================
def _add_media(app_module, db_session, pair, **kwargs):
    fields = dict(video_filename="extra.mp4", sort_order=1, is_default=False)
    fields.update(kwargs)
    media = app_module.PairMedia(pair_id=pair.id, **fields)
    db_session.add(media)
    db_session.commit()
    return media


def test_single_media_preview_unchanged(client, app_module, db_session, login_user, project_with_pair):
    project, pair = project_with_pair
    resp = client.get(f"/project/{project.id}/preview")
    assert resp.status_code == 200
    assert "Videos (" not in resp.get_data(as_text=True)


def test_three_media_preview_shows_all_labeled_in_order(client, app_module, db_session, login_user, project_with_pair):
    project, pair = project_with_pair
    v1 = _add_media(app_module, db_session, pair, video_filename=pair.video_filename, sort_order=0, is_default=True)
    v2 = _add_media(app_module, db_session, pair, video_filename="extra1.mp4", sort_order=1, is_default=False)
    v3 = _add_media(app_module, db_session, pair, video_filename="extra2.mp4", sort_order=2, is_default=False)
    Path(app_module.VIDEOS_DIR).mkdir(parents=True, exist_ok=True)
    (Path(app_module.VIDEOS_DIR) / "extra1.mp4").write_bytes(b"x")
    (Path(app_module.VIDEOS_DIR) / "extra2.mp4").write_bytes(b"x")

    resp = client.get(f"/project/{project.id}/preview")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Videos (3)" in html
    assert "Video 1" in html and "Video 2" in html and "Video 3" in html
    assert "Default" in html
    assert f"/video/{project.id}/{pair.pair_index}/media/{v1.id}" in html
    assert f"/video/{project.id}/{pair.pair_index}/media/{v2.id}" in html
    assert f"/video/{project.id}/{pair.pair_index}/media/{v3.id}" in html
    # no autoplay on the management screen, no filesystem paths leaked
    assert "autoplay" not in html.lower()
    assert str(app_module.VIDEOS_DIR).replace("\\", "/") not in html.replace("\\", "/")


def test_multi_media_preview_admin_uses_admin_route(app_module, db_session, admin):
    # Renders the template directly (mirrors the existing 3E-E payload tests'
    # test_request_context() pattern) rather than through the full
    # admin_view route auth dance, which is unrelated to the template fix
    # under test here (owner-aware media URL selection).
    project = app_module.Project(name="Admin Preview MV", owner_admin_id=admin.id, user_project_index=1)
    db_session.add(project)
    db_session.commit()
    pair = app_module.ProjectPair(
        project_id=project.id, pair_index=0, image_filename="x.jpg", video_filename="x.mp4",
        is_processed=True, processing_status="completed", feature_extraction_status="extracted",
    )
    db_session.add(pair)
    db_session.commit()
    _add_media(app_module, db_session, pair, video_filename="x.mp4", sort_order=0, is_default=True)
    _add_media(app_module, db_session, pair, video_filename="y.mp4", sort_order=1, is_default=False)
    db_session.expire_all()
    project = app_module.Project.query.get(project.id)
    pair = app_module.ProjectPair.query.get(pair.id)

    with app_module.app.test_request_context():
        html = app_module.render_template(
            "user/project_preview.html", user=None, project=project, pairs=[pair], admin_view=True,
            share_url="https://example.test/s/x", coverage={}, ownership={},
        )
    assert f"/admin/video/{project.id}/0/media/" in html
    assert f'src="/video/{project.id}/0/media/' not in html  # not the non-admin route


# ===========================================================================
# 3. iPhone Safari login zoom-on-focus regression
# ===========================================================================
def _login_html():
    return Path("templates/user/login.html").read_text(encoding="utf-8", errors="ignore")


def test_login_input_font_size_stays_at_or_above_16px_in_mobile_media_query():
    html = _login_html()
    block = html[html.index("@media (max-width: 768px)"):html.index("@media (max-width: 480px)")]
    start = block.index(".input-gloss {")
    input_gloss = block[start:block.index("}", start)]
    import re
    m = re.search(r"font-size:\s*(\d+)px", input_gloss)
    assert m, "expected an explicit font-size in the mobile .input-gloss rule"
    assert int(m.group(1)) >= 16, "under 16px triggers iOS Safari auto-zoom-on-focus"


def test_password_toggle_button_is_type_button_and_does_not_clear_value():
    html = _login_html()
    assert 'id="togglePassword"' in html
    toggle_block = html[html.index('id="togglePassword"') - 60: html.index('id="togglePassword"') + 40]
    assert 'type="button"' in toggle_block  # never submits the form on tap

    assert "passwordInput.value" not in html  # toggle JS never touches .value
    assert "passwordInput.type = revealing ? 'text' : 'password'" in html
    assert "passwordInput.focus()" in html  # focus restored after toggle


def test_password_toggle_tap_target_is_at_least_40px():
    html = _login_html()
    start = html.index(".toggle-password {")
    block = html[start:html.index("}", start)]
    import re
    w = re.search(r"width:\s*(\d+)px", block)
    h = re.search(r"height:\s*(\d+)px", block)
    assert w and int(w.group(1)) >= 40
    assert h and int(h.group(1)) >= 40


# ===========================================================================
# 4. Support email replacement
# ===========================================================================
def test_no_user_facing_old_support_email_remains():
    import subprocess
    result = subprocess.run(
        ["git", "grep", "-l", "contact@myscanstory.com"],
        cwd=str(Path(__file__).resolve().parents[2]), capture_output=True, text=True,
    )
    assert result.returncode == 1, f"old address still present in: {result.stdout}"  # 1 = no matches


def test_new_support_email_present_in_key_user_facing_pages():
    for path in ("templates/user/contact.html", "templates/user/landing.html", "templates/user/terms.html"):
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        assert "connect@myscanstory.com" in text, path
