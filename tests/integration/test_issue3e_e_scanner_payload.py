"""Issue 3E-E: scanner multi-video payload (backend).

The live scanner reads video URLs from /detect_init's JSON response, not from
the server-rendered target list (that list is Jinja-only, feeding the
static target-guide thumbnails and Direct QR's single video src - see
_render_scanner_project). Achieving a genuine "detected": true response
through the real endpoint requires a real ORB/homography match, which no
test in this suite attempts (grep confirms every existing /detect_init test
asserts detected is False) - real-camera correctness for THIS phase is
verified separately via manual browser QA. These tests instead cover the
payload-building unit directly (_pair_media_payload) and the read-side
routes it points at, which is where the actual URL/ownership/leakage
correctness lives.
"""
from pathlib import Path


def _add_media(app_module, db_session, pair, **kwargs):
    fields = dict(video_filename="extra.mp4", sort_order=1, is_default=False)
    fields.update(kwargs)
    media = app_module.PairMedia(pair_id=pair.id, **fields)
    db_session.add(media)
    db_session.commit()
    return media


def _payload(app_module, pair, endpoint, external=False):
    # _pair_media_payload calls url_for, which needs a request context (not
    # just an app context) unless SERVER_NAME is configured.
    with app_module.app.test_request_context():
        return app_module._pair_media_payload(pair, endpoint, external=external)


# ===========================================================================
# 1-6: _pair_media_payload
# ===========================================================================
def test_legacy_pair_with_no_pairmedia_returns_none(app_module, project_with_pair):
    _project, pair = project_with_pair
    assert pair.media_items == []
    assert _payload(app_module, pair, "serve_pair_media_video") is None


def test_one_pairmedia_payload_is_backward_compatible(app_module, db_session, project_with_pair):
    _project, pair = project_with_pair
    media = _add_media(app_module, db_session, pair, video_filename="only.mp4", sort_order=0, is_default=True)
    payload = _payload(app_module, pair, "serve_pair_media_video")
    # v=media.video_size (cache-busting, issue 5) - fixture never sets video_size, so v=0.
    assert payload == [{
        "id": media.id,
        "video_url": f"/video/{pair.project_id}/{pair.pair_index}/media/{media.id}?v=0",
        "sort_order": 0,
        "is_default": True,
    }]


def test_multi_pairmedia_ordered_sort_order_then_id(app_module, db_session, project_with_pair):
    _project, pair = project_with_pair
    third = _add_media(app_module, db_session, pair, video_filename="c.mp4", sort_order=2, is_default=False)
    first = _add_media(app_module, db_session, pair, video_filename="a.mp4", sort_order=0, is_default=True)
    second = _add_media(app_module, db_session, pair, video_filename="b.mp4", sort_order=1, is_default=False)
    db_session.expire_all()
    refreshed = app_module.ProjectPair.query.get(pair.id)

    payload = _payload(app_module, refreshed, "serve_pair_media_video")
    assert [item["id"] for item in payload] == [first.id, second.id, third.id]
    assert [item["sort_order"] for item in payload] == [0, 1, 2]


def test_default_media_is_first_in_the_ordered_list(app_module, db_session, project_with_pair):
    _project, pair = project_with_pair
    default = _add_media(app_module, db_session, pair, video_filename="default.mp4", sort_order=0, is_default=True)
    _add_media(app_module, db_session, pair, video_filename="extra.mp4", sort_order=1, is_default=False)
    db_session.expire_all()
    refreshed = app_module.ProjectPair.query.get(pair.id)

    payload = _payload(app_module, refreshed, "serve_pair_media_video")
    assert payload[0]["id"] == default.id
    assert payload[0]["is_default"] is True


def test_admin_owned_pair_uses_the_admin_media_endpoint(app_module, db_session, admin):
    project = app_module.Project(name="Admin 3E-E Payload", owner_admin_id=admin.id, user_project_index=1)
    db_session.add(project)
    db_session.commit()
    pair = app_module.ProjectPair(
        project_id=project.id, pair_index=0, image_filename="x.jpg", video_filename="x.mp4",
        is_processed=True, processing_status="completed", feature_extraction_status="extracted",
    )
    db_session.add(pair)
    db_session.commit()
    media = _add_media(app_module, db_session, pair, video_filename="default.mp4", sort_order=0, is_default=True)

    payload = _payload(app_module, pair, "serve_admin_pair_media_video")
    assert payload[0]["video_url"] == f"/admin/video/{project.id}/0/media/{media.id}?v=0"


def test_media_urls_carry_no_filesystem_paths_or_raw_filenames(app_module, db_session, project_with_pair):
    _project, pair = project_with_pair
    _add_media(app_module, db_session, pair, video_filename="secret_original_name.mp4", sort_order=0, is_default=True)
    db_session.expire_all()
    refreshed = app_module.ProjectPair.query.get(pair.id)

    payload = _payload(app_module, refreshed, "serve_pair_media_video")
    url = payload[0]["video_url"]
    assert "secret_original_name" not in url
    assert "\\" not in url and str(Path(app_module.VIDEOS_DIR)) not in url
    assert url.startswith(f"/video/{pair.project_id}/{pair.pair_index}/media/")


# ===========================================================================
# Read-side routes the payload points at
# ===========================================================================
def test_serve_pair_media_video_route_serves_the_correct_bytes(client, app_module, db_session, project_with_pair, tmp_path):
    project, pair = project_with_pair
    video_path = Path(app_module.VIDEOS_DIR) / "extra_media.mp4"
    video_path.write_bytes(b"extra video bytes")
    media = _add_media(app_module, db_session, pair, video_filename=video_path.name, sort_order=1, is_default=False)

    resp = client.get(f"/video/{project.id}/{pair.pair_index}/media/{media.id}")
    assert resp.status_code == 200
    assert resp.data == b"extra video bytes"


def test_serve_pair_media_video_rejects_media_belonging_to_a_different_pair(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    other_pair = app_module.ProjectPair(
        project_id=project.id, pair_index=1, image_filename="o.jpg", video_filename="o.mp4",
        is_processed=True, processing_status="completed", feature_extraction_status="extracted",
    )
    db_session.add(other_pair)
    db_session.commit()
    media = _add_media(app_module, db_session, other_pair, video_filename="o_extra.mp4", sort_order=1, is_default=False)

    # media belongs to other_pair (pair_index=1), requested under pair_index=0's URL
    resp = client.get(f"/video/{project.id}/{pair.pair_index}/media/{media.id}")
    assert resp.status_code == 404


def test_serve_admin_pair_media_video_rejects_a_user_owned_project(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    media = _add_media(app_module, db_session, pair, video_filename="x.mp4", sort_order=1, is_default=False)

    resp = client.get(f"/admin/video/{project.id}/{pair.pair_index}/media/{media.id}")
    assert resp.status_code == 404


# ===========================================================================
# detect_init wiring - structural backstop for the path no test can exercise
# live (see module docstring).
# ===========================================================================
def test_detect_init_response_wires_matched_media_additively():
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    fn = source[source.index("def detect_init("):source.index("def detect_track(")]
    assert 'matched_media = _pair_media_payload(matched_pair, matched_media_endpoint, external=True)' in fn
    assert '**({"media": matched_media} if matched_media else {})' in fn
    # video_url stays exactly as it was - additive, never replaced.
    assert '"video_url": matched_video_url,' in fn
