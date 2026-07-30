from pathlib import Path


def test_test_mode_marker_and_sqlite_database(app_module):
    assert app_module.app.config["TESTING"] is True
    assert app_module.app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///")


def test_storage_roots_are_inside_tmp(app_module, isolated_app):
    tmp_root = Path(isolated_app[2]).resolve()
    for raw in [
        app_module.DATA_DIR, app_module.IMAGES_DIR, app_module.VIDEOS_DIR, app_module.FEATURES_DIR,
        app_module.QR_DIR, app_module.ADMIN_DATA_DIR, app_module.ADMIN_IMAGES_DIR,
        app_module.ADMIN_VIDEOS_DIR, app_module.ADMIN_FEATURES_DIR, app_module.ADMIN_QR_DIR,
        app_module.STATIC_UPLOADS_DIR,
    ]:
        path = Path(raw).resolve()
        assert path == tmp_root or tmp_root in path.parents


def test_external_http_calls_are_blocked(app_module):
    import pytest

    with pytest.raises(AssertionError, match="Unmocked external HTTP call blocked"):
        app_module.requests.post("https://example.com")


def test_smtp_calls_are_blocked(app_module):
    import pytest
    import smtplib

    with pytest.raises(AssertionError, match="Unmocked SMTP call blocked"):
        smtplib.SMTP("smtp.example.com")


def test_relationship_cleanup_removes_project_pairs(app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    db_session.delete(project)
    db_session.commit()
    assert app_module.ProjectPair.query.filter_by(id=pair.id).first() is None


def test_unique_project_pair_constraint(app_module, db_session, project_with_pair):
    import pytest
    from sqlalchemy.exc import IntegrityError

    project, pair = project_with_pair
    duplicate = app_module.ProjectPair(
        project_id=project.id,
        pair_index=pair.pair_index,
        image_filename="dup.jpg",
        video_filename="dup.mp4",
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
