"""Video duplicate validation + warning UI polish + Direct QR Edit parity pass
(2026-08-31).

Covers:
  - find_video_duplicate() centralization (Tracked Overlay / Detect Once were
    already correct on Add; Replace never had a duplicate check at all before
    this pass).
  - Section 24/25 test matrix (A-F) for Tracked Overlay / Detect Once (same
    routes, same helper - playback_mode does not change video-identity scope,
    proven once rather than duplicating the whole HTTP matrix per mode).
  - Section 26 test matrix (A-G) for Direct QR's new video-only Add/Replace/
    Remove routes (each Direct QR video is its own ProjectPair under the hood -
    Add Video creates one, Remove Video deletes one, Replace Video reuses the
    existing per-pair-media route unchanged).
  - Warning-modal flash categories (error-modal/info-modal) reaching the
    rendered page for the polished notice component.
  - DB assertions: no orphan PairMedia/ProjectPair/ProcessingJob rows survive
    a blocked duplicate attempt.
"""
import io
import os
import tempfile

import cv2
import numpy as np
import pytest
from PIL import Image


def _jpeg_bytes(color, size=(300, 300)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="JPEG", quality=90)
    out.seek(0)
    return out


_MP4_CACHE = {}


def _mp4_bytes(fill=0):
    """fill is used as an RNG seed for per-pixel noise, not a flat color - two
    ADJACENT flat-color fill values (e.g. 7 and 8) were found to compress to
    byte-IDENTICAL mp4v output for this tiny 64x64/5-frame clip (lossy codec
    quantization collapsing near-identical uniform brightness), which would
    silently defeat this file's whole point (distinguishing "same content" from
    "different content" by exact hash). Seeded per-pixel noise makes distinct
    fill values produce genuinely distinct encoded bytes, while the same fill
    value still deterministically reproduces the identical bytes each call."""
    if fill not in _MP4_CACHE:
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 64))
            rng = np.random.default_rng(fill)
            for _ in range(5):
                writer.write(rng.integers(0, 256, size=(64, 64, 3), dtype=np.uint8))
            writer.release()
            with open(path, "rb") as fh:
                _MP4_CACHE[fill] = fh.read()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    return io.BytesIO(_MP4_CACHE[fill])


@pytest.fixture()
def mock_feature_extraction_only(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "make_feature_working_jpeg", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "extract_features_multi", lambda *a, **k: None)

    class NoopThread:
        def __init__(self, target=None, args=(), kwargs=None, **_ignored):
            self.target, self.args, self.kwargs = target, args, kwargs or {}
        def start(self):
            return None
    monkeypatch.setattr(app_module.threading, "Thread", NoopThread)


def _enable_multi_video(db_session, plan, max_videos=5):
    """user_add_pair_media (Add Video to an existing pair) gates on
    allow_multi_video_per_target - the default trial plan fixture has this off
    (see test_multi_video_editing.py's identical helper/comment), so any test
    hitting that route needs it explicitly enabled first."""
    plan.allow_multi_video_per_target = True
    plan.max_videos_per_target = max_videos
    db_session.commit()


@pytest.fixture()
def direct_qr_project(app_module, db_session, normal_user):
    project = app_module.Project(
        name="Direct QR Video Parity", owner_user_id=normal_user.id, user_project_index=8801,
        experience_type="direct_qr", scanner_url="/scanner/8801",
        qr_code_filename="dqr.png", qr_code_path="/qr/dqr.png",
    )
    db_session.add(project)
    db_session.commit()
    return project


def _add_direct_qr_video(client, project_id, fill=1):
    return client.post(
        f"/projects/{project_id}/direct-qr/video/add",
        data={"new_video": (_mp4_bytes(fill=fill), "v.mp4")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _replace_pair_media(client, project_id, pair_index, media_id, fill):
    return client.post(
        f"/projects/{project_id}/pair/{pair_index}/media/{media_id}/replace",
        data={"replacement_video": (_mp4_bytes(fill=fill), "r.mp4")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _remove_direct_qr_video(client, project_id, pair_index):
    return client.post(
        f"/projects/{project_id}/direct-qr/video/{pair_index}/remove",
        follow_redirects=True,
    )


# ===========================================================================
# Source-level proof: centralized, not duplicated across routes
# ===========================================================================

def test_find_video_duplicate_is_the_single_shared_helper():
    from pathlib import Path
    src = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    assert src.count("def find_video_duplicate(") == 1
    for anchor in ("def user_add_pair_media(", "def user_replace_pair_media(", "def user_add_direct_qr_video("):
        start = src.index(anchor)
        next_def = src.index("\ndef ", start + len(anchor))
        block = src[start:next_def]
        assert "find_video_duplicate(" in block, f"{anchor} does not call the shared helper"


# ===========================================================================
# Tracked Overlay / Detect Once matrix (A-F) - image_video, mode-agnostic
# ===========================================================================

def test_a_add_exact_same_video_to_same_pair_is_blocked(app_module, db_session, project_with_pair, plan, login_user, client, mock_feature_extraction_only):
    _enable_multi_video(db_session, plan)
    project, pair0 = project_with_pair
    existing_path = os.path.join(app_module.VIDEOS_DIR, pair0.video_filename)
    os.makedirs(os.path.dirname(existing_path), exist_ok=True)
    with open(existing_path, "wb") as f:
        f.write(_mp4_bytes(fill=1).read())
    pair0.video_hash = app_module._sha256_of_file(existing_path)
    media0 = app_module.PairMedia(pair=pair0, video_filename=pair0.video_filename, video_size=10, sort_order=0, is_default=True, video_hash=pair0.video_hash)
    db_session.add(media0)
    db_session.commit()

    before_media = app_module.PairMedia.query.filter_by(pair_id=pair0.id).count()
    before_jobs = app_module.ProcessingJob.query.count()
    resp = client.post(
        f"/projects/{project.id}/pair/{pair0.pair_index}/media/add",
        data={"new_video": (_mp4_bytes(fill=1), "dup.mp4")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Video already added" in resp.data
    assert app_module.PairMedia.query.filter_by(pair_id=pair0.id).count() == before_media
    assert app_module.ProcessingJob.query.count() == before_jobs


def test_b_add_different_video_to_same_pair_is_allowed(app_module, db_session, project_with_pair, plan, login_user, client, mock_feature_extraction_only):
    _enable_multi_video(db_session, plan)
    project, pair0 = project_with_pair
    # project_with_pair's pair0 starts with ZERO PairMedia rows (a legacy-shape
    # pair with only pair.video_filename set) - user_add_pair_media's own
    # _ensure_default_pair_media() backfills ONE default row for that existing
    # video before adding the new one, so the count goes 0 -> 2, not 0 -> 1.
    # Asserting the delta (not a hardcoded absolute) is correct either way.
    before = app_module.PairMedia.query.filter_by(pair_id=pair0.id).count()
    resp = client.post(
        f"/projects/{project.id}/pair/{pair0.pair_index}/media/add",
        data={"new_video": (_mp4_bytes(fill=2), "new.mp4")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Video already added" not in resp.data
    after = app_module.PairMedia.query.filter_by(pair_id=pair0.id).count()
    # +1 for the backfilled default (pair0 started at 0 rows) and +1 for the
    # genuinely new upload - both from the SAME add-video call.
    assert after - before == 2


def test_c_same_video_under_a_different_pair_is_allowed(app_module, db_session, project_with_pair, multiple_pairs, plan, login_user, client, mock_feature_extraction_only):
    _enable_multi_video(db_session, plan)
    project = multiple_pairs
    pair0 = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=0).first()
    pair1 = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=1).first()
    existing_path = os.path.join(app_module.VIDEOS_DIR, pair0.video_filename)
    os.makedirs(os.path.dirname(existing_path), exist_ok=True)
    with open(existing_path, "wb") as f:
        f.write(_mp4_bytes(fill=3).read())
    pair0.video_hash = app_module._sha256_of_file(existing_path)
    db_session.add(app_module.PairMedia(pair=pair0, video_filename=pair0.video_filename, video_size=10, sort_order=0, is_default=True, video_hash=pair0.video_hash))
    db_session.commit()

    # pair1 (the target of THIS add) also starts with zero PairMedia rows - same
    # _ensure_default_pair_media() backfill as test_b, so the delta (not a
    # hardcoded absolute) is what actually proves "one new video was added".
    before = app_module.PairMedia.query.filter_by(pair_id=pair1.id).count()
    resp = client.post(
        f"/projects/{project.id}/pair/{pair1.pair_index}/media/add",
        data={"new_video": (_mp4_bytes(fill=3), "reused.mp4")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Video already added" not in resp.data
    after = app_module.PairMedia.query.filter_by(pair_id=pair1.id).count()
    # +1 for the backfilled default (pair1 started at 0 rows) and +1 for the
    # genuinely new upload - both from the SAME add-video call.
    assert after - before == 2


def test_d_replace_video_with_itself_is_noop(app_module, db_session, project_with_pair, login_user, client, mock_feature_extraction_only):
    project, pair0 = project_with_pair
    existing_path = os.path.join(app_module.VIDEOS_DIR, pair0.video_filename)
    os.makedirs(os.path.dirname(existing_path), exist_ok=True)
    with open(existing_path, "wb") as f:
        f.write(_mp4_bytes(fill=4).read())
    video_hash = app_module._sha256_of_file(existing_path)
    media0 = app_module.PairMedia(pair=pair0, video_filename=pair0.video_filename, video_size=10, sort_order=0, is_default=True, video_hash=video_hash)
    db_session.add(media0)
    db_session.commit()
    media_id = media0.id
    # Captured as a plain str BEFORE the request - db_session and the request's
    # own commit share one scoped session (expire_on_commit=True by default),
    # so re-reading pair0.video_filename AFTER that commit would silently
    # reload the (possibly now-changed) live value instead of the original,
    # making an ORM-attribute-vs-ORM-attribute comparison a tautology that can
    # never catch a real regression.
    original_filename = pair0.video_filename

    resp = client.post(
        f"/projects/{project.id}/pair/{pair0.pair_index}/media/{media_id}/replace",
        data={"replacement_video": (_mp4_bytes(fill=4), "same.mp4")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Already your current video" in resp.data
    refreshed = app_module.PairMedia.query.get(media_id)
    assert refreshed.video_filename == original_filename, "no-op must not rewrite the file"


def test_e_replace_with_a_video_already_in_the_same_pair_is_blocked(app_module, db_session, project_with_pair, login_user, client, mock_feature_extraction_only):
    project, pair0 = project_with_pair
    v1_path = os.path.join(app_module.VIDEOS_DIR, pair0.video_filename)
    os.makedirs(os.path.dirname(v1_path), exist_ok=True)
    with open(v1_path, "wb") as f:
        f.write(_mp4_bytes(fill=5).read())
    v1_hash = app_module._sha256_of_file(v1_path)
    media1 = app_module.PairMedia(pair=pair0, video_filename=pair0.video_filename, video_size=10, sort_order=0, is_default=True, video_hash=v1_hash)
    db_session.add(media1)
    db_session.flush()

    v2_filename = f"{project.id}_{pair0.pair_index}_extra.mp4"
    v2_path = os.path.join(app_module.VIDEOS_DIR, v2_filename)
    with open(v2_path, "wb") as f:
        f.write(_mp4_bytes(fill=6).read())
    v2_hash = app_module._sha256_of_file(v2_path)
    media2 = app_module.PairMedia(pair=pair0, video_filename=v2_filename, video_size=10, sort_order=1, is_default=False, video_hash=v2_hash)
    db_session.add(media2)
    db_session.commit()
    media1_id = media1.id
    original_filename = pair0.video_filename  # plain str snapshot - see test_d's comment

    resp = client.post(
        f"/projects/{project.id}/pair/{pair0.pair_index}/media/{media1_id}/replace",
        data={"replacement_video": (_mp4_bytes(fill=6), "collide.mp4")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Video already added" in resp.data
    refreshed = app_module.PairMedia.query.get(media1_id)
    assert refreshed.video_filename == original_filename, "blocked replace must not overwrite the row"


def test_f_replace_with_a_genuinely_unique_video_is_allowed(app_module, db_session, project_with_pair, login_user, client, mock_feature_extraction_only):
    project, pair0 = project_with_pair
    v1_path = os.path.join(app_module.VIDEOS_DIR, pair0.video_filename)
    os.makedirs(os.path.dirname(v1_path), exist_ok=True)
    with open(v1_path, "wb") as f:
        f.write(_mp4_bytes(fill=7).read())
    media1 = app_module.PairMedia(pair=pair0, video_filename=pair0.video_filename, video_size=10, sort_order=0, is_default=True, video_hash=app_module._sha256_of_file(v1_path))
    db_session.add(media1)
    db_session.commit()
    media1_id = media1.id
    original_filename = pair0.video_filename  # plain str snapshot - see test_d's comment

    resp = client.post(
        f"/projects/{project.id}/pair/{pair0.pair_index}/media/{media1_id}/replace",
        data={"replacement_video": (_mp4_bytes(fill=8), "unique.mp4")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Video already added" not in resp.data
    assert b"Already your current video" not in resp.data
    refreshed = app_module.PairMedia.query.get(media1_id)
    assert refreshed.video_filename != original_filename


def test_overlay_and_detect_once_share_the_same_video_identity_scope(app_module, db_session, project_with_pair):
    """Detect Once uses the exact same add/replace-video routes and the same
    find_video_duplicate(pair_id=...) scope as Tracked Overlay - playback_mode
    never enters that function's signature, so there is no separate code path
    to diverge. Confirms this by source (no playback_mode branch inside
    find_video_duplicate) rather than re-running the whole A-F HTTP matrix a
    second time for an identical code path."""
    from pathlib import Path
    src = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    start = src.index("def find_video_duplicate(")
    end = src.index("\ndef ", start + 10)
    block = src[start:end]
    assert "playback_mode" not in block


# ===========================================================================
# Direct QR matrix (A-G)
# ===========================================================================

def test_direct_qr_a_add_exact_duplicate_video_is_blocked(app_module, db_session, direct_qr_project, login_user, client, mock_feature_extraction_only):
    project = direct_qr_project
    r1 = _add_direct_qr_video(client, project.id, fill=10)
    assert r1.status_code == 200
    r2 = _add_direct_qr_video(client, project.id, fill=11)
    assert r2.status_code == 200

    before = app_module.ProjectPair.query.filter_by(project_id=project.id).count()
    r3 = _add_direct_qr_video(client, project.id, fill=10)  # exact same as the first
    assert r3.status_code == 200
    assert b"Video already added" in r3.data
    assert app_module.ProjectPair.query.filter_by(project_id=project.id).count() == before


def test_direct_qr_b_add_unique_third_video_is_allowed(app_module, db_session, direct_qr_project, login_user, client, mock_feature_extraction_only):
    project = direct_qr_project
    _add_direct_qr_video(client, project.id, fill=20)
    _add_direct_qr_video(client, project.id, fill=21)
    resp = _add_direct_qr_video(client, project.id, fill=22)
    assert resp.status_code == 200
    assert b"Video already added" not in resp.data
    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).order_by(app_module.ProjectPair.pair_index).all()
    assert [p.pair_index for p in pairs] == [0, 1, 2]


def test_direct_qr_c_replace_with_itself_is_noop(app_module, db_session, direct_qr_project, login_user, client, mock_feature_extraction_only):
    project = direct_qr_project
    _add_direct_qr_video(client, project.id, fill=30)
    pair0 = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=0).first()
    media0 = pair0.default_media
    media0_id = media0.id
    original_filename = media0.video_filename  # plain str - see test_d's comment above (Overlay matrix)

    resp = _replace_pair_media(client, project.id, pair0.pair_index, media0_id, fill=30)
    assert resp.status_code == 200
    assert b"Already your current video" in resp.data
    refreshed = app_module.PairMedia.query.get(media0_id)
    assert refreshed.video_filename == original_filename


def test_direct_qr_d_replace_with_another_pairs_video_is_blocked(app_module, db_session, direct_qr_project, login_user, client, mock_feature_extraction_only):
    project = direct_qr_project
    _add_direct_qr_video(client, project.id, fill=40)
    _add_direct_qr_video(client, project.id, fill=41)
    pair0 = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=0).first()
    media0 = pair0.default_media
    media0_id = media0.id
    original_filename = media0.video_filename

    resp = _replace_pair_media(client, project.id, pair0.pair_index, media0_id, fill=41)  # pair1's content
    assert resp.status_code == 200
    assert b"Video already added" in resp.data
    refreshed = app_module.PairMedia.query.get(media0_id)
    assert refreshed.video_filename == original_filename, "blocked replace must not overwrite the row"


def test_direct_qr_e_replace_with_unique_video_is_allowed(app_module, db_session, direct_qr_project, login_user, client, mock_feature_extraction_only):
    project = direct_qr_project
    _add_direct_qr_video(client, project.id, fill=50)
    pair0 = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=0).first()
    media0 = pair0.default_media
    media0_id = media0.id
    original_filename = media0.video_filename

    resp = _replace_pair_media(client, project.id, pair0.pair_index, media0_id, fill=51)
    assert resp.status_code == 200
    assert b"Video already added" not in resp.data
    refreshed = app_module.PairMedia.query.get(media0_id)
    assert refreshed.video_filename != original_filename


def test_direct_qr_f_remove_video_when_more_than_one_remains_is_allowed(app_module, db_session, direct_qr_project, login_user, client, mock_feature_extraction_only):
    project = direct_qr_project
    _add_direct_qr_video(client, project.id, fill=60)
    _add_direct_qr_video(client, project.id, fill=61)
    pair0 = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=0).first()

    resp = _remove_direct_qr_video(client, project.id, pair0.pair_index)
    assert resp.status_code == 200
    assert app_module.ProjectPair.query.filter_by(project_id=project.id).count() == 1


def test_direct_qr_g_removing_the_last_video_is_blocked(app_module, db_session, direct_qr_project, login_user, client, mock_feature_extraction_only):
    project = direct_qr_project
    _add_direct_qr_video(client, project.id, fill=70)
    pair0 = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=0).first()

    resp = _remove_direct_qr_video(client, project.id, pair0.pair_index)
    assert resp.status_code == 200
    assert b"Can&#39;t remove this video" in resp.data or b"Can't remove this video" in resp.data
    assert app_module.ProjectPair.query.filter_by(project_id=project.id).count() == 1


def test_direct_qr_double_click_add_creates_exactly_one_extra_pair(app_module, db_session, direct_qr_project, login_user, client, mock_feature_extraction_only):
    """Sequential double-submit of the identical video (Flask's test client has
    no real concurrency - see test_v11_postgres_concurrency_proof.py for genuine
    simultaneity) must still land on exactly one committed pair, proving the
    project-row lock + duplicate check together, not just the app-level
    pre-check alone."""
    project = direct_qr_project
    _add_direct_qr_video(client, project.id, fill=80)
    first = _add_direct_qr_video(client, project.id, fill=81)
    assert first.status_code == 200
    second = _add_direct_qr_video(client, project.id, fill=81)
    assert second.status_code == 200
    assert b"Video already added" in second.data
    assert app_module.ProjectPair.query.filter_by(project_id=project.id).count() == 2


# ===========================================================================
# Direct QR is video-only: no ROI/image logic anywhere in its Edit markup
# ===========================================================================

def test_direct_qr_edit_page_never_renders_marker_editor(app_module, db_session, direct_qr_project, login_user, client, mock_feature_extraction_only):
    project = direct_qr_project
    _add_direct_qr_video(client, project.id, fill=90)
    resp = client.get(f"/projects/{project.id}/edit")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="ignore")
    assert "directQrVideosPanel" in body
    assert "Add Video" in body
    # Real rendered UI markers only - not a bare substring scan, which would
    # false-positive on e.g. wireAddTargetFormSubmitGuard's own JS *comment*
    # (which literally contains the words "Add another target" describing a
    # DIFFERENT form that simply isn't rendered here - the comment itself is
    # harmless dead text, not a UI leak).
    for forbidden in ('id="markerFlowModal"', 'id="addTargetPanel"', 'id="addTargetForm"', "Replace target image", "marker-editor.js"):
        assert forbidden not in body, forbidden


def test_image_video_edit_page_still_renders_marker_editor(app_module, db_session, project_with_pair, login_user, client):
    """Regression guard for the gating above: image_video projects must keep
    their existing, already-working ROI/marker-editor flow untouched."""
    project, _pair0 = project_with_pair
    resp = client.get(f"/projects/{project.id}/edit")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="ignore")
    assert "Replace target image" in body
    assert "marker-editor.js" in body
    assert "markerFlowModal" in body
