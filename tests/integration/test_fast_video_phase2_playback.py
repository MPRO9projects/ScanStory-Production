"""Fast Video Phase 2: playback preference for optimized PairMedia
derivatives, with the original always as the safe, trusted fallback.

Covers the resolver (resolve_pair_media_filename /
resolve_pair_default_video_filename), the four owner-aware serving routes
it feeds (user/admin x default/PairMedia), HTTP Range behavior on both
branches, and the invariant that no optimized derivative can ever become a
hard dependency - "ready" in the DB is never trusted alone, only a file
that still exists on disk is ever served.
"""
from pathlib import Path


def _add_media(app_module, db_session, pair, **kwargs):
    fields = dict(video_filename="extra.mp4", sort_order=1, is_default=False)
    fields.update(kwargs)
    media = app_module.PairMedia(pair_id=pair.id, **fields)
    db_session.add(media)
    db_session.commit()
    return media


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _make_admin_pair(app_module, db_session, admin, index=0):
    project = app_module.Project(name="Admin FV2 Project", owner_admin_id=admin.id, user_project_index=1)
    db_session.add(project)
    db_session.commit()
    pair = app_module.ProjectPair(
        project_id=project.id, pair_index=index, image_filename="x.jpg", video_filename=f"{project.id}_{index}.mp4",
        is_processed=True, processing_status="completed", feature_extraction_status="extracted",
    )
    db_session.add(pair)
    db_session.commit()
    return project, pair


# ===========================================================================
# 1-5: resolver state matrix (optimization_status x file-existence)
# ===========================================================================
def test_pending_status_serves_original(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    orig = _write(Path(app_module.VIDEOS_DIR) / "pending_orig.mp4", b"original bytes")
    media = _add_media(app_module, db_session, pair, video_filename=orig.name, sort_order=0, is_default=True,
                        optimization_status="pending")
    resp = client.get(f"/video/{project.id}/{pair.pair_index}/media/{media.id}")
    assert resp.status_code == 200
    assert resp.data == b"original bytes"


def test_processing_status_serves_original(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    orig = _write(Path(app_module.VIDEOS_DIR) / "processing_orig.mp4", b"original bytes")
    media = _add_media(app_module, db_session, pair, video_filename=orig.name, sort_order=0, is_default=True,
                        optimization_status="processing")
    resp = client.get(f"/video/{project.id}/{pair.pair_index}/media/{media.id}")
    assert resp.data == b"original bytes"


def test_failed_status_serves_original(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    orig = _write(Path(app_module.VIDEOS_DIR) / "failed_orig.mp4", b"original bytes")
    media = _add_media(app_module, db_session, pair, video_filename=orig.name, sort_order=0, is_default=True,
                        optimization_status="failed", optimization_error="not smaller than original")
    resp = client.get(f"/video/{project.id}/{pair.pair_index}/media/{media.id}")
    assert resp.data == b"original bytes"


def test_ready_but_missing_derivative_file_serves_original(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    orig = _write(Path(app_module.VIDEOS_DIR) / "ready_missing_orig.mp4", b"original bytes")
    media = _add_media(
        app_module, db_session, pair, video_filename=orig.name, sort_order=0, is_default=True,
        optimization_status="ready", optimized_video_filename="ghost_derivative_never_written.mp4",
    )
    resp = client.get(f"/video/{project.id}/{pair.pair_index}/media/{media.id}")
    assert resp.status_code == 200
    assert resp.data == b"original bytes"


def test_ready_and_existing_derivative_serves_optimized(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    orig = _write(Path(app_module.VIDEOS_DIR) / "ready_orig.mp4", b"original bytes")
    derivative = _write(Path(app_module.VIDEOS_DIR) / "ready_derivative.mp4", b"optimized bytes")
    media = _add_media(
        app_module, db_session, pair, video_filename=orig.name, sort_order=0, is_default=True,
        optimization_status="ready", optimized_video_filename=derivative.name,
    )
    resp = client.get(f"/video/{project.id}/{pair.pair_index}/media/{media.id}")
    assert resp.status_code == 200
    assert resp.data == b"optimized bytes"


# ===========================================================================
# 6-8: routes (default/user, PairMedia/user, PairMedia/admin)
# ===========================================================================
def test_default_route_prefers_optimized_when_default_media_matches_pair_video(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    derivative = _write(Path(app_module.VIDEOS_DIR) / "default_route_derivative.mp4", b"optimized default bytes")
    _add_media(
        app_module, db_session, pair, video_filename=pair.video_filename, sort_order=0, is_default=True,
        optimization_status="ready", optimized_video_filename=derivative.name,
    )
    resp = client.get(f"/video/{project.id}/{pair.pair_index}")
    assert resp.status_code == 200
    assert resp.data == b"optimized default bytes"


def test_non_default_pairmedia_optimized_route(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    orig = _write(Path(app_module.VIDEOS_DIR) / "extra_orig.mp4", b"extra original")
    derivative = _write(Path(app_module.VIDEOS_DIR) / "extra_derivative.mp4", b"extra optimized")
    media = _add_media(
        app_module, db_session, pair, video_filename=orig.name, sort_order=1, is_default=False,
        optimization_status="ready", optimized_video_filename=derivative.name,
    )
    resp = client.get(f"/video/{project.id}/{pair.pair_index}/media/{media.id}")
    assert resp.data == b"extra optimized"


def test_admin_pairmedia_optimized_route(client, app_module, db_session, admin):
    project, pair = _make_admin_pair(app_module, db_session, admin)
    orig = _write(Path(app_module.ADMIN_VIDEOS_DIR) / "admin_orig.mp4", b"admin original")
    derivative = _write(Path(app_module.ADMIN_VIDEOS_DIR) / "admin_derivative.mp4", b"admin optimized")
    media = _add_media(app_module, db_session, pair, video_filename=orig.name, sort_order=0, is_default=True,
                        optimization_status="ready", optimized_video_filename=derivative.name)
    resp = client.get(f"/admin/video/{project.id}/{pair.pair_index}/media/{media.id}")
    assert resp.data == b"admin optimized"


# ===========================================================================
# 9: no filesystem path exposure
# ===========================================================================
def test_optimized_serving_leaks_no_filesystem_path(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    orig = _write(Path(app_module.VIDEOS_DIR) / "leak_orig.mp4", b"o")
    derivative = _write(Path(app_module.VIDEOS_DIR) / "leak_derivative.mp4", b"d")
    media = _add_media(app_module, db_session, pair, video_filename=orig.name, sort_order=0, is_default=True,
                        optimization_status="ready", optimized_video_filename=derivative.name)
    resp = client.get(f"/video/{project.id}/{pair.pair_index}/media/{media.id}")
    assert resp.headers.get("Content-Disposition") == "inline"

    missing = client.get(f"/video/{project.id}/{pair.pair_index}/media/999999")
    assert missing.status_code == 404
    assert str(Path(app_module.VIDEOS_DIR)) not in missing.get_data(as_text=True)
    assert derivative.name not in missing.get_data(as_text=True)


# ===========================================================================
# 10-15: Range support, original and optimized, user/admin/PairMedia
# ===========================================================================
def test_user_default_route_original_range_206(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    resp = client.get(f"/video/{project.id}/{pair.pair_index}", headers={"Range": "bytes=0-3"})
    assert resp.status_code == 206
    assert resp.headers["Content-Range"].startswith("bytes 0-3/")


def test_user_default_route_optimized_range_206(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    derivative = _write(Path(app_module.VIDEOS_DIR) / "range_default_derivative.mp4", b"optimized range bytes long enough")
    _add_media(app_module, db_session, pair, video_filename=pair.video_filename, sort_order=0, is_default=True,
               optimization_status="ready", optimized_video_filename=derivative.name)
    resp = client.get(f"/video/{project.id}/{pair.pair_index}", headers={"Range": "bytes=0-3"})
    assert resp.status_code == 206
    assert resp.headers["Content-Range"].startswith("bytes 0-3/")


def test_admin_default_route_original_range_206(client, app_module, db_session, admin):
    project, pair = _make_admin_pair(app_module, db_session, admin)
    _write(Path(app_module.ADMIN_VIDEOS_DIR) / pair.video_filename, b"admin original range bytes")
    resp = client.get(f"/admin/video/{project.id}/{pair.pair_index}", headers={"Range": "bytes=0-3"})
    assert resp.status_code == 206
    assert resp.headers["Content-Range"].startswith("bytes 0-3/")


def test_admin_default_route_optimized_range_206(client, app_module, db_session, admin):
    project, pair = _make_admin_pair(app_module, db_session, admin)
    _write(Path(app_module.ADMIN_VIDEOS_DIR) / pair.video_filename, b"admin original")
    derivative = _write(Path(app_module.ADMIN_VIDEOS_DIR) / "admin_range_derivative.mp4", b"admin optimized range bytes")
    _add_media(app_module, db_session, pair, video_filename=pair.video_filename, sort_order=0, is_default=True,
               optimization_status="ready", optimized_video_filename=derivative.name)
    resp = client.get(f"/admin/video/{project.id}/{pair.pair_index}", headers={"Range": "bytes=0-3"})
    assert resp.status_code == 206
    assert resp.headers["Content-Range"].startswith("bytes 0-3/")


def test_pairmedia_route_original_range_206(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    orig = _write(Path(app_module.VIDEOS_DIR) / "range_media_orig.mp4", b"pair media original range bytes")
    media = _add_media(app_module, db_session, pair, video_filename=orig.name, sort_order=1, is_default=False)
    resp = client.get(f"/video/{project.id}/{pair.pair_index}/media/{media.id}", headers={"Range": "bytes=0-3"})
    assert resp.status_code == 206
    assert resp.headers["Content-Range"].startswith("bytes 0-3/")


def test_pairmedia_route_optimized_range_206(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    orig = _write(Path(app_module.VIDEOS_DIR) / "range_media_orig2.mp4", b"orig")
    derivative = _write(Path(app_module.VIDEOS_DIR) / "range_media_derivative.mp4", b"optimized range media bytes")
    media = _add_media(app_module, db_session, pair, video_filename=orig.name, sort_order=1, is_default=False,
                        optimization_status="ready", optimized_video_filename=derivative.name)
    resp = client.get(f"/video/{project.id}/{pair.pair_index}/media/{media.id}", headers={"Range": "bytes=0-3"})
    assert resp.status_code == 206
    assert resp.headers["Content-Range"].startswith("bytes 0-3/")


# ===========================================================================
# 25-26: legacy / one-video compatibility
# ===========================================================================
def test_legacy_pair_without_pairmedia_unaffected_by_resolver(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    assert pair.media_items == []
    resp = client.get(f"/video/{project.id}/{pair.pair_index}")
    assert resp.status_code == 200
    assert resp.data == b"fake video"


def test_resolver_returns_pair_video_filename_when_no_pairmedia(app_module, db_session, project_with_pair):
    _project, pair = project_with_pair
    assert app_module.resolve_pair_default_video_filename(pair, app_module.VIDEOS_DIR) == pair.video_filename


# ===========================================================================
# 27-28: multi-video sequence with a mix of optimized/original media
# ===========================================================================
def test_mixed_optimized_and_original_sequence_serves_correct_bytes_independently(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    v1_orig = _write(Path(app_module.VIDEOS_DIR) / "seq1_orig.mp4", b"v1 orig")
    v1_derivative = _write(Path(app_module.VIDEOS_DIR) / "seq1_derivative.mp4", b"v1 optimized")
    m1 = _add_media(app_module, db_session, pair, video_filename=v1_orig.name, sort_order=0, is_default=True,
                     optimization_status="ready", optimized_video_filename=v1_derivative.name)

    v2_orig = _write(Path(app_module.VIDEOS_DIR) / "seq2_orig.mp4", b"v2 orig")
    m2 = _add_media(app_module, db_session, pair, video_filename=v2_orig.name, sort_order=1, is_default=False,
                     optimization_status="pending")

    v3_orig = _write(Path(app_module.VIDEOS_DIR) / "seq3_orig.mp4", b"v3 orig")
    v3_derivative = _write(Path(app_module.VIDEOS_DIR) / "seq3_derivative.mp4", b"v3 optimized")
    m3 = _add_media(app_module, db_session, pair, video_filename=v3_orig.name, sort_order=2, is_default=False,
                     optimization_status="ready", optimized_video_filename=v3_derivative.name)

    r1 = client.get(f"/video/{project.id}/{pair.pair_index}/media/{m1.id}")
    r2 = client.get(f"/video/{project.id}/{pair.pair_index}/media/{m2.id}")
    r3 = client.get(f"/video/{project.id}/{pair.pair_index}/media/{m3.id}")
    assert r1.data == b"v1 optimized"
    assert r2.data == b"v2 orig"  # no ready derivative -> original fallback, no mixing with m1/m3
    assert r3.data == b"v3 optimized"

    # 3E-E payload contract (URL shape) is untouched by optimization state -
    # the scanner never needs to know which branch a media_id resolves to.
    db_session.expire_all()
    refreshed = app_module.ProjectPair.query.get(pair.id)
    with app_module.app.test_request_context():
        payload = app_module._pair_media_payload(refreshed, "serve_pair_media_video")
    assert [p["video_url"] for p in payload] == [
        f"/video/{project.id}/{pair.pair_index}/media/{m1.id}",
        f"/video/{project.id}/{pair.pair_index}/media/{m2.id}",
        f"/video/{project.id}/{pair.pair_index}/media/{m3.id}",
    ]


# ===========================================================================
# 30-31: stale-file safety and repair
# ===========================================================================
def test_manually_deleted_derivative_falls_back_safely_no_404(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    orig = _write(Path(app_module.VIDEOS_DIR) / "stale_orig.mp4", b"original stale")
    derivative = _write(Path(app_module.VIDEOS_DIR) / "stale_derivative.mp4", b"will be deleted")
    media = _add_media(app_module, db_session, pair, video_filename=orig.name, sort_order=0, is_default=True,
                        optimization_status="ready", optimized_video_filename=derivative.name)
    derivative.unlink()  # DB still says ready - simulates a file deleted out from under it

    resp = client.get(f"/video/{project.id}/{pair.pair_index}/media/{media.id}")
    assert resp.status_code == 200
    assert resp.data == b"original stale"


def test_retry_after_missing_derivative_restores_optimized_preference(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    orig = _write(Path(app_module.VIDEOS_DIR) / "restore_orig.mp4", b"orig restore")
    derivative_path = Path(app_module.VIDEOS_DIR) / "restore_derivative.mp4"
    media = _add_media(app_module, db_session, pair, video_filename=orig.name, sort_order=0, is_default=True,
                        optimization_status="ready", optimized_video_filename=derivative_path.name)

    before = client.get(f"/video/{project.id}/{pair.pair_index}/media/{media.id}")
    assert before.data == b"orig restore"  # derivative not yet on disk -> original

    _write(derivative_path, b"restored optimized")  # e.g. Phase 1's idempotent repair re-writes it
    after = client.get(f"/video/{project.id}/{pair.pair_index}/media/{media.id}")
    assert after.data == b"restored optimized"
