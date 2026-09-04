"""V1.1 multi-video editing completion: add/replace/remove/reorder/set-default
for individual PairMedia rows on an existing target, via the user_edit_project
family of routes.

Media fixtures are locally duplicated (real cv2-generated mp4 bytes) rather
than imported across test modules, matching the documented convention
elsewhere in this suite: a stray global site-packages `tests` package shadows
dotted `tests.xxx` imports in this environment.
"""
import os
import tempfile
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import pytest


def _mp4_bytes(width=64, height=64, frames=5):
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (width, height))
        for _ in range(frames):
            writer.write(np.zeros((height, width, 3), dtype=np.uint8))
        writer.release()
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _enable_multi_video(db_session, plan, max_videos=5):
    plan.allow_multi_video_per_target = True
    plan.max_videos_per_target = max_videos
    db_session.commit()


def _add_media(app_module, db_session, pair, **kwargs):
    fields = dict(video_filename="extra.mp4", sort_order=1, is_default=False)
    fields.update(kwargs)
    media = app_module.PairMedia(pair_id=pair.id, **fields)
    db_session.add(media)
    db_session.commit()
    return media


def _post_video(client, url, field, data_bytes, filename="clip.mp4", **extra):
    payload = {field: (BytesIO(data_bytes), filename)}
    payload.update(extra)
    return client.post(url, data=payload, content_type="multipart/form-data", follow_redirects=False)


# ===========================================================================
# A. one-media edit baseline / B. 3-media edit page shows all media
# ===========================================================================
def test_one_media_edit_baseline_unchanged(client, app_module, db_session, login_user, project_with_pair):
    project, _pair = project_with_pair
    resp = client.get(f"/projects/{project.id}/edit")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Replace video" in html  # classic single-video slot still present
    assert "Videos (1)" in html


def test_three_media_edit_page_shows_all_media_in_order(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    Path(app_module.VIDEOS_DIR).mkdir(parents=True, exist_ok=True)
    (Path(app_module.VIDEOS_DIR) / "extra1.mp4").write_bytes(b"x")
    (Path(app_module.VIDEOS_DIR) / "extra2.mp4").write_bytes(b"x")
    v1 = _add_media(app_module, db_session, pair, video_filename=pair.video_filename, sort_order=0, is_default=True)
    v2 = _add_media(app_module, db_session, pair, video_filename="extra1.mp4", sort_order=1, is_default=False)
    v3 = _add_media(app_module, db_session, pair, video_filename="extra2.mp4", sort_order=2, is_default=False)

    resp = client.get(f"/projects/{project.id}/edit")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Videos (3)" in html
    assert "Video 1" in html and "Video 2" in html and "Video 3" in html
    assert "Default" in html
    assert 'for="video_0"' not in html  # classic single-video slot hidden once >1 media
    assert f"/video/{project.id}/{pair.pair_index}/media/{v1.id}" in html
    assert f"/video/{project.id}/{pair.pair_index}/media/{v2.id}" in html
    assert f"/video/{project.id}/{pair.pair_index}/media/{v3.id}" in html
    assert str(app_module.VIDEOS_DIR).replace("\\", "/") not in html.replace("\\", "/")  # no path leakage


# ===========================================================================
# C. add second/third media
# ===========================================================================
def test_add_second_media_creates_pair_media_and_job(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair

    resp = _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    assert resp.status_code == 302
    db_session.expire_all()
    pair = app_module.ProjectPair.query.get(pair.id)
    media_rows = sorted(pair.media_items, key=lambda m: m.sort_order)
    assert len(media_rows) == 2
    assert media_rows[0].is_default is True
    assert media_rows[1].is_default is False
    assert media_rows[1].sort_order == 1
    job = app_module.active_project_job(project.id, job_type="optimize_pair_media", pair_media_id=media_rows[1].id)
    assert job is not None


def test_add_third_media_gets_next_sort_order(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    resp = _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes(frames=6))
    assert resp.status_code == 302
    db_session.expire_all()
    media_rows = app_module.PairMedia.query.filter_by(pair_id=pair.id).order_by(app_module.PairMedia.sort_order).all()
    assert [m.sort_order for m in media_rows] == [0, 1, 2]


# ===========================================================================
# D. entitlement limit enforced / E. add when not entitled rejected
# ===========================================================================
def test_add_beyond_plan_limit_rejected(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=2)
    project, pair = project_with_pair
    assert _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes()).status_code == 302
    resp = _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    assert resp.status_code == 302
    db_session.expire_all()
    media_rows = app_module.PairMedia.query.filter_by(pair_id=pair.id).all()
    assert len(media_rows) == 2  # the second add was rejected, not silently allowed


def test_add_when_not_entitled_rejected(client, app_module, db_session, login_user, project_with_pair):
    project, pair = project_with_pair  # plan defaults to allow_multi_video_per_target=False
    resp = _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    assert resp.status_code == 302
    db_session.expire_all()
    assert app_module.PairMedia.query.filter_by(pair_id=pair.id).count() == 0


# ===========================================================================
# F. replace Video 2 only / G. replace default Video 1
# ===========================================================================
def test_replace_video_2_leaves_1_and_3_untouched(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    # Distinct frame counts: _mp4_bytes() is fully deterministic on its
    # default args, and the two added videos must be genuinely different
    # content, not just different rows - identical content here would now
    # correctly trip the exact-duplicate-video guard (physical QA fix).
    for frames in (5, 6):
        _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes(frames=frames))
    db_session.expire_all()
    pair = app_module.ProjectPair.query.get(pair.id)
    media_rows = sorted(pair.media_items, key=lambda m: m.sort_order)
    v1_name, v3_name = media_rows[0].video_filename, media_rows[2].video_filename
    v2_id, v2_name_before = media_rows[1].id, media_rows[1].video_filename

    resp = _post_video(
        client, f"/projects/{project.id}/pair/{pair.pair_index}/media/{v2_id}/replace",
        "replacement_video", _mp4_bytes(frames=7),
    )
    assert resp.status_code == 302
    db_session.expire_all()
    refreshed = app_module.PairMedia.query.get(v2_id)
    assert refreshed.video_filename != v2_name_before  # new physical name
    assert refreshed.optimization_status == "pending"  # Fast Video state reset
    assert refreshed.optimized_video_filename is None
    unchanged = app_module.PairMedia.query.filter(app_module.PairMedia.id.in_([media_rows[0].id, media_rows[2].id])).all()
    assert {m.video_filename for m in unchanged} == {v1_name, v3_name}


def test_replace_default_video_1_updates_legacy_mirror(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    db_session.expire_all()
    pair = app_module.ProjectPair.query.get(pair.id)
    default_media = pair.default_media

    resp = _post_video(
        client, f"/projects/{project.id}/pair/{pair.pair_index}/media/{default_media.id}/replace",
        "replacement_video", _mp4_bytes(frames=8),
    )
    assert resp.status_code == 302
    db_session.expire_all()
    refreshed_pair = app_module.ProjectPair.query.get(pair.id)
    refreshed_media = app_module.PairMedia.query.get(default_media.id)
    assert refreshed_pair.video_filename == refreshed_media.video_filename  # legacy mirror follows


# ===========================================================================
# H. remove non-default media / I. cannot remove final media
# ===========================================================================
def test_remove_non_default_media(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    db_session.expire_all()
    pair = app_module.ProjectPair.query.get(pair.id)
    extra = next(m for m in pair.media_items if not m.is_default)
    video_path = Path(app_module.VIDEOS_DIR) / extra.video_filename
    assert video_path.exists()

    resp = client.post(f"/projects/{project.id}/pair/{pair.pair_index}/media/{extra.id}/remove", follow_redirects=False)
    assert resp.status_code == 302
    db_session.expire_all()
    assert app_module.PairMedia.query.get(extra.id) is None
    assert not video_path.exists()


def test_remove_detaches_processing_job_reference_instead_of_violating_fk(client, app_module, db_session, login_user, normal_user, project_with_pair):
    """Real-topology field QA (Postgres, which enforces FKs - unlike this
    suite's SQLite, which silently allows a dangling reference) caught a
    genuine bug here: ProcessingJob.pair_media_id has no ON DELETE clause,
    and every PairMedia gets an auto-enqueued optimize_pair_media job
    pointing at it - deleting the PairMedia outright violated that FK.
    Fixed by detaching (not deleting) the referencing job row first, the
    same soft-preserve-history convention the storage ledger already uses."""
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    db_session.expire_all()
    pair = app_module.ProjectPair.query.get(pair.id)
    extra = next(m for m in pair.media_items if not m.is_default)
    job = app_module.ProcessingJob.query.filter_by(pair_media_id=extra.id, job_type="optimize_pair_media").first()
    assert job is not None  # auto-enqueued by the add above

    resp = client.post(f"/projects/{project.id}/pair/{pair.pair_index}/media/{extra.id}/remove", follow_redirects=False)
    assert resp.status_code == 302  # not a crash / malformed response
    db_session.expire_all()
    refreshed_job = app_module.ProcessingJob.query.get(job.id)
    assert refreshed_job is not None  # job history preserved, not deleted
    assert refreshed_job.pair_media_id is None  # detached, not left dangling


def test_cannot_remove_final_media(client, app_module, db_session, login_user, normal_user, project_with_pair):
    project, pair = project_with_pair
    default_media = app_module._ensure_default_pair_media(pair)
    db_session.commit()
    resp = client.post(f"/projects/{project.id}/pair/{pair.pair_index}/media/{default_media.id}/remove", follow_redirects=False)
    assert resp.status_code == 302
    db_session.expire_all()
    assert app_module.PairMedia.query.get(default_media.id) is not None


def test_cannot_remove_default_media_directly(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    db_session.expire_all()
    pair = app_module.ProjectPair.query.get(pair.id)
    default_media = pair.default_media
    resp = client.post(f"/projects/{project.id}/pair/{pair.pair_index}/media/{default_media.id}/remove", follow_redirects=False)
    assert resp.status_code == 302
    db_session.expire_all()
    assert app_module.PairMedia.query.get(default_media.id) is not None  # still there


# ===========================================================================
# J. default change preserves exactly one default / K. legacy mirror follows
# ===========================================================================
def test_set_default_preserves_exactly_one_default_and_updates_mirror(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    db_session.expire_all()
    pair = app_module.ProjectPair.query.get(pair.id)
    old_default = pair.default_media
    new_default = next(m for m in pair.media_items if not m.is_default)

    resp = client.post(f"/projects/{project.id}/pair/{pair.pair_index}/media/{new_default.id}/set-default", follow_redirects=False)
    assert resp.status_code == 302
    db_session.expire_all()
    all_media = app_module.PairMedia.query.filter_by(pair_id=pair.id).all()
    defaults = [m for m in all_media if m.is_default]
    assert len(defaults) == 1
    assert defaults[0].id == new_default.id
    refreshed_pair = app_module.ProjectPair.query.get(pair.id)
    refreshed_new_default = app_module.PairMedia.query.get(new_default.id)
    assert refreshed_pair.video_filename == refreshed_new_default.video_filename
    old_default_refreshed = app_module.PairMedia.query.get(old_default.id)
    assert old_default_refreshed.is_default is False


# ===========================================================================
# L. reorder persists / M. scanner payload order follows reorder
# ===========================================================================
def test_reorder_persists_and_scanner_payload_follows(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    # Distinct frame counts: see test_replace_video_2_leaves_1_and_3_untouched
    # for why bare repeated _mp4_bytes() calls are no longer valid here.
    for frames in (5, 6):
        _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes(frames=frames))
    db_session.expire_all()
    pair = app_module.ProjectPair.query.get(pair.id)
    ordered = sorted(pair.media_items, key=lambda m: m.sort_order)
    v1, v2, v3 = ordered

    resp = client.post(
        f"/projects/{project.id}/pair/{pair.pair_index}/media/{v2.id}/move",
        data={"direction": "up"}, follow_redirects=False,
    )
    assert resp.status_code == 302
    db_session.expire_all()
    reordered = app_module.PairMedia.query.filter_by(pair_id=pair.id).order_by(app_module.PairMedia.sort_order).all()
    assert [m.id for m in reordered] == [v2.id, v1.id, v3.id]

    with app_module.app.test_request_context():
        payload = app_module._pair_media_payload(app_module.ProjectPair.query.get(pair.id), "serve_pair_media_video")
    assert [p["id"] for p in payload] == [v2.id, v1.id, v3.id]


# ===========================================================================
# N. Fast Video status resets on replacement / O. optimize job enqueued once
# ===========================================================================
def test_replace_resets_fast_video_status_and_enqueues_once(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    db_session.expire_all()
    pair = app_module.ProjectPair.query.get(pair.id)
    media = next(m for m in pair.media_items if not m.is_default)
    media.optimization_status = "ready"
    media.optimized_video_filename = "stale_optimized.mp4"
    db_session.commit()

    resp = _post_video(
        client, f"/projects/{project.id}/pair/{pair.pair_index}/media/{media.id}/replace",
        "replacement_video", _mp4_bytes(frames=9),
    )
    assert resp.status_code == 302
    db_session.expire_all()
    refreshed = app_module.PairMedia.query.get(media.id)
    assert refreshed.optimization_status == "pending"
    assert refreshed.optimized_video_filename is None
    jobs = app_module.ProcessingJob.query.filter_by(pair_media_id=media.id, job_type="optimize_pair_media").all()
    assert len(jobs) == 1  # exactly one job for the replacement, not a second stray one


# ===========================================================================
# P. derivative cleanup on replace/remove
# ===========================================================================
def test_replace_deletes_old_original_and_old_derivative_files(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    db_session.expire_all()
    pair = app_module.ProjectPair.query.get(pair.id)
    media = next(m for m in pair.media_items if not m.is_default)
    old_video_path = Path(app_module.VIDEOS_DIR) / media.video_filename
    assert old_video_path.exists()
    old_optimized_path = Path(app_module.VIDEOS_DIR) / "old_derivative_for_replace.mp4"
    old_optimized_path.write_bytes(b"derivative")
    media.optimized_video_filename = old_optimized_path.name
    db_session.commit()

    resp = _post_video(
        client, f"/projects/{project.id}/pair/{pair.pair_index}/media/{media.id}/replace",
        "replacement_video", _mp4_bytes(frames=10),
    )
    assert resp.status_code == 302
    assert not old_video_path.exists()
    assert not old_optimized_path.exists()


def test_remove_deletes_original_and_derivative_files(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    db_session.expire_all()
    pair = app_module.ProjectPair.query.get(pair.id)
    media = next(m for m in pair.media_items if not m.is_default)
    optimized_path = Path(app_module.VIDEOS_DIR) / "removable_derivative.mp4"
    optimized_path.write_bytes(b"derivative")
    media.optimized_video_filename = optimized_path.name
    db_session.commit()
    original_path = Path(app_module.VIDEOS_DIR) / media.video_filename

    resp = client.post(f"/projects/{project.id}/pair/{pair.pair_index}/media/{media.id}/remove", follow_redirects=False)
    assert resp.status_code == 302
    assert not original_path.exists()
    assert not optimized_path.exists()


# ===========================================================================
# Q. storage accounting correct
# ===========================================================================
def test_add_bills_exactly_one_media_object(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    before = app_module.MediaObject.query.filter_by(project_id=project.id, status="ACTIVE").count()
    _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    after = app_module.MediaObject.query.filter_by(project_id=project.id, status="ACTIVE").count()
    assert after == before + 1


def test_replace_supersedes_old_object_and_records_exactly_one_new(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    db_session.expire_all()
    pair = app_module.ProjectPair.query.get(pair.id)
    media = next(m for m in pair.media_items if not m.is_default)
    active_before = app_module.MediaObject.query.filter_by(project_id=project.id, status="ACTIVE").count()

    _post_video(
        client, f"/projects/{project.id}/pair/{pair.pair_index}/media/{media.id}/replace",
        "replacement_video", _mp4_bytes(frames=11),
    )
    db_session.expire_all()
    active_after = app_module.MediaObject.query.filter_by(project_id=project.id, status="ACTIVE").count()
    assert active_after == active_before  # one superseded, one new - net zero
    superseded = app_module.MediaObject.query.filter_by(project_id=project.id, status="SUPERSEDED").count()
    assert superseded == 1


def test_remove_releases_storage_accounting(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    db_session.expire_all()
    pair = app_module.ProjectPair.query.get(pair.id)
    media = next(m for m in pair.media_items if not m.is_default)

    active_before = app_module.MediaObject.query.filter_by(project_id=project.id, status="ACTIVE").count()
    client.post(f"/projects/{project.id}/pair/{pair.pair_index}/media/{media.id}/remove", follow_redirects=False)
    db_session.expire_all()
    active_after = app_module.MediaObject.query.filter_by(project_id=project.id, status="ACTIVE").count()
    deleted = app_module.MediaObject.query.filter_by(project_id=project.id, status="DELETED").count()
    assert deleted == 1
    assert active_after == active_before - 1  # exactly the removed one's row released


# ===========================================================================
# R. retry/idempotency does not duplicate media/jobs/storage
# ===========================================================================
def test_add_retry_after_success_does_not_duplicate(client, app_module, db_session, login_user, normal_user, project_with_pair):
    """Simulates a client double-submit (network hiccup, double-tap): two
    independent add requests must produce two independent media rows (this
    endpoint has no idempotency KEY the way upload finalize does - each POST
    is a genuinely new add), but neither request may duplicate the OTHER's
    job/storage row - proven by exactly one job per resulting media id."""
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    project, pair = project_with_pair
    _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes(frames=6))
    db_session.expire_all()
    pair = app_module.ProjectPair.query.get(pair.id)
    non_default = [m for m in pair.media_items if not m.is_default]
    assert len(non_default) == 2
    for m in non_default:
        jobs = app_module.ProcessingJob.query.filter_by(pair_media_id=m.id, job_type="optimize_pair_media").all()
        assert len(jobs) == 1


# ===========================================================================
# S. cross-owner mutation rejected
# ===========================================================================
def test_cross_owner_cannot_add_media_to_someone_elses_project(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    other = app_module.User(
        email="other-editor@example.com", first_name="Other", last_name="Editor",
        password_hash="x", is_verified=True, subscription_status="trial",
        projects_used=0, scans_used=0,
    )
    db_session.add(other)
    db_session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = other.id
    resp = _post_video(client, f"/projects/{project.id}/pair/{pair.pair_index}/media/add", "new_video", _mp4_bytes())
    assert resp.status_code == 404
    assert app_module.PairMedia.query.filter_by(pair_id=pair.id).count() == 0


def test_cross_pair_media_id_scoping_rejected(client, app_module, db_session, login_user, normal_user, project_with_pair):
    _enable_multi_video(db_session, normal_user.subscription_plan)
    project, pair = project_with_pair
    other_pair = app_module.ProjectPair(
        project_id=project.id, pair_index=1, image_filename="o.jpg", video_filename="o.mp4",
        is_processed=True, processing_status="completed", feature_extraction_status="extracted",
    )
    db_session.add(other_pair)
    db_session.commit()
    other_media = app_module.PairMedia(pair_id=other_pair.id, video_filename="o.mp4", sort_order=0, is_default=True)
    db_session.add(other_media)
    db_session.commit()

    # media belongs to other_pair (pair_index=1), requested under pair_index=0's URL
    resp = client.post(f"/projects/{project.id}/pair/{pair.pair_index}/media/{other_media.id}/remove", follow_redirects=False)
    assert resp.status_code == 404


# ===========================================================================
# T. legacy one-video project unaffected
# ===========================================================================
def test_legacy_one_video_project_edit_flow_unaffected(client, app_module, db_session, login_user, project_with_pair):
    project, pair = project_with_pair
    assert pair.media_items == []
    resp = client.get(f"/projects/{project.id}/edit")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'name="video_0"' in html  # classic replace slot present, unaffected
    assert "Add video" not in html  # not entitled by default, so no add affordance
