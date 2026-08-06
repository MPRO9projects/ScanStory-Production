"""V1 Wave 6: fallback-video data model + fallback analytics/event
classification.

Covers: matched-scan/fallback-event distinguishability, fallback events
never counting as successful scans, pair-level vs project-level fallback
resolution, idempotent duplicate event submission (DB-constraint backed),
cross-project/cross-user access-control isolation, and suspended-project
blocking via the existing _project_is_available() mechanism.
"""
import uuid
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash


def _make_pair(app_module, db_session, project, index, with_video=True):
    """Creates a real ProjectPair with an actual on-disk video file (so
    ProjectPair.can_serve_video is True) - project_with_pair/multiple_pairs
    in conftest.py only guarantee a real file for pair_index 0."""
    image_path = Path(app_module.IMAGES_DIR) / f"{project.id}_{index}.jpg"
    video_path = Path(app_module.VIDEOS_DIR) / f"{project.id}_{index}.mp4"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"fake image")
    if with_video:
        video_path.write_bytes(b"fake video")
    pair = app_module.ProjectPair(
        project_id=project.id,
        pair_index=index,
        image_filename=image_path.name,
        video_filename=video_path.name,
        is_processed=True,
        processing_status="completed",
        feature_extraction_status="extracted",
    )
    db_session.add(pair)
    db_session.commit()
    return pair


def _other_user_project_with_pair(app_module, db_session, plan, index=0):
    other_user = app_module.User(
        email="other-owner@example.com",
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_id=plan.id,
        subscription_status="trial",
    )
    db_session.add(other_user)
    db_session.commit()
    other_project = app_module.Project(
        name="Other Owner Project",
        owner_user_id=other_user.id,
        is_active=True,
    )
    db_session.add(other_project)
    db_session.commit()
    other_pair = _make_pair(app_module, db_session, other_project, index)
    return other_user, other_project, other_pair


# ---------------------------------------------------------------------
# Matched scan vs fallback event distinguishability / never-counted-as-success
# ---------------------------------------------------------------------

def test_matched_scan_counted_once_and_distinguishable_from_fallback_events(
    client, app_module, db_session, normal_user, project_with_pair
):
    project, pair = project_with_pair
    scan = app_module.ScanLog(
        project_id=project.id,
        pair_id=pair.id,
        user_id=normal_user.id,
        scan_session_id="matched-session",
        is_successful=True,
        counted=False,
    )
    db_session.add(scan)
    db_session.commit()

    # A fallback event on the SAME project must never appear in ScanLog.
    resp = client.post(
        f"/api/scanner/{project.id}/fallback-event",
        json={"event_type": "recognition_timeout", "client_event_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 201

    end = client.post("/api/scanner/session/end", json={"project_id": project.id, "session_id": "matched-session"})
    assert end.get_json()["counted"] is True

    assert app_module.ScanLog.query.filter_by(project_id=project.id).count() == 1
    assert app_module.ScanLog.query.filter_by(project_id=project.id, is_successful=True).count() == 1
    assert app_module.ScanEvent.query.filter_by(project_id=project.id).count() == 1
    # Never mixed into the same table/row space.
    matched_row = app_module.ScanLog.query.filter_by(project_id=project.id).first()
    assert not hasattr(matched_row, "event_type")


def test_fallback_events_never_counted_as_successful_scan(client, app_module, db_session, normal_user, project_with_pair):
    project, pair = project_with_pair
    session_id = "fallback-only-session"

    for event_type in ("recognition_timeout", "camera_unavailable", "project_fallback_view", "pair_fallback_view"):
        resp = client.post(
            f"/api/scanner/{project.id}/fallback-event",
            json={
                "event_type": event_type,
                "client_event_id": str(uuid.uuid4()),
                "scan_session_id": session_id,
                "pair_index": pair.pair_index if event_type == "pair_fallback_view" else None,
            },
        )
        assert resp.status_code == 201, resp.get_json()

    # None of this ever created a ScanLog row - the existing "successful
    # scans" aggregation (admin dashboard's ScanLog.query.filter_by(...).count()
    # / ScanLog.query.count(), project.scan_count) is structurally untouched.
    assert app_module.ScanLog.query.filter_by(project_id=project.id).count() == 0
    assert app_module.ScanLog.query.count() == 0
    assert app_module.ScanEvent.query.filter_by(project_id=project.id).count() == 4

    # scanner_session_end (the existing single counting authority) finds no
    # successful scan for this session and correctly refuses to count it.
    end = client.post("/api/scanner/session/end", json={"project_id": project.id, "session_id": session_id})
    payload = end.get_json()
    assert payload["ok"] is True
    assert payload["counted"] is False
    assert payload["reason"] == "No successful detection"


def test_public_endpoint_rejects_matched_scan_as_an_event_type(client, project_with_pair):
    project, _pair = project_with_pair
    resp = client.post(
        f"/api/scanner/{project.id}/fallback-event",
        json={"event_type": "matched_scan", "client_event_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload["success"] is False
    assert payload["code"] == "INVALID_EVENT_TYPE"


# ---------------------------------------------------------------------
# Pair-level vs project-level fallback video resolution
# ---------------------------------------------------------------------

def test_pair_level_fallback_event_records_pair_reference(client, app_module, project_with_pair):
    project, pair = project_with_pair
    event_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/scanner/{project.id}/fallback-event",
        json={"event_type": "pair_fallback_view", "client_event_id": event_id, "pair_index": pair.pair_index},
    )
    assert resp.status_code == 201
    row = app_module.ScanEvent.query.filter_by(client_event_id=event_id).first()
    assert row.event_type == "pair_fallback_view"
    assert row.pair_id == pair.id


def test_project_level_fallback_event_has_no_pair_reference(client, app_module, project_with_pair):
    project, _pair = project_with_pair
    event_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/scanner/{project.id}/fallback-event",
        json={"event_type": "project_fallback_view", "client_event_id": event_id},
    )
    assert resp.status_code == 201
    row = app_module.ScanEvent.query.filter_by(client_event_id=event_id).first()
    assert row.event_type == "project_fallback_view"
    assert row.pair_id is None


def test_fallback_video_unavailable_when_nothing_configured(client, project_with_pair):
    project, _pair = project_with_pair
    resp = client.get(f"/api/scanner/{project.id}/fallback-video")
    assert resp.status_code == 200
    assert resp.get_json() == {"available": False}


def test_fallback_video_pair_hint_resolves_to_that_pairs_own_video(client, project_with_pair):
    project, pair = project_with_pair
    resp = client.get(f"/api/scanner/{project.id}/fallback-video?pair_index={pair.pair_index}")
    payload = resp.get_json()
    assert payload["available"] is True
    assert payload["source"] == "pair"
    assert payload["pair_index"] == pair.pair_index
    assert f"/video/{project.id}/{pair.pair_index}" in payload["video_url"]


def test_fallback_video_falls_back_to_project_default_without_pair_hint(
    client, app_module, db_session, project_with_pair
):
    project, pair = project_with_pair
    project.fallback_pair_id = pair.id
    db_session.commit()

    resp = client.get(f"/api/scanner/{project.id}/fallback-video")
    payload = resp.get_json()
    assert payload["available"] is True
    assert payload["source"] == "project_default"
    assert payload["pair_index"] == pair.pair_index


def test_fallback_video_pair_hint_preferred_over_project_default(client, app_module, db_session, project_with_pair):
    project, pair0 = project_with_pair
    pair1 = _make_pair(app_module, db_session, project, index=1)
    project.fallback_pair_id = pair0.id  # project default points at pair 0
    db_session.commit()

    # A pair-context hint for pair 1 must win over the project-level default.
    resp = client.get(f"/api/scanner/{project.id}/fallback-video?pair_index=1")
    payload = resp.get_json()
    assert payload["available"] is True
    assert payload["source"] == "pair"
    assert payload["pair_index"] == 1


def test_fallback_video_skips_pair_with_no_actual_video_file(client, app_module, db_session, project_with_pair):
    project, _pair0 = project_with_pair
    pair_no_video = _make_pair(app_module, db_session, project, index=1, with_video=False)
    resp = client.get(f"/api/scanner/{project.id}/fallback-video?pair_index={pair_no_video.pair_index}")
    assert resp.get_json() == {"available": False}


# ---------------------------------------------------------------------
# Idempotent duplicate event submission
# ---------------------------------------------------------------------

def test_duplicate_fallback_event_submission_is_idempotent(client, app_module, project_with_pair):
    project, _pair = project_with_pair
    event_id = str(uuid.uuid4())
    body = {"event_type": "recognition_timeout", "client_event_id": event_id, "scan_session_id": "retry-session"}

    first = client.post(f"/api/scanner/{project.id}/fallback-event", json=body)
    second = client.post(f"/api/scanner/{project.id}/fallback-event", json=body)

    assert first.status_code == 201
    assert first.get_json()["duplicate"] is False
    assert second.status_code == 200
    assert second.get_json()["duplicate"] is True
    assert app_module.ScanEvent.query.filter_by(client_event_id=event_id).count() == 1


def test_client_event_id_unique_constraint_rejects_raw_duplicate_insert(app_module, db_session, project_with_pair):
    """Proves the idempotency guarantee is DB-enforced, not just an
    in-app check - a raw duplicate INSERT (bypassing the route entirely)
    must also fail."""
    project, _pair = project_with_pair
    event_id = str(uuid.uuid4())
    db_session.add(app_module.ScanEvent(
        project_id=project.id, event_type="recognition_timeout", client_event_id=event_id,
    ))
    db_session.commit()

    from sqlalchemy.exc import IntegrityError
    db_session.add(app_module.ScanEvent(
        project_id=project.id, event_type="camera_unavailable", client_event_id=event_id,
    ))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_missing_client_event_id_rejected(client, project_with_pair):
    project, _pair = project_with_pair
    resp = client.post(
        f"/api/scanner/{project.id}/fallback-event",
        json={"event_type": "recognition_timeout"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "MISSING_CLIENT_EVENT_ID"


# ---------------------------------------------------------------------
# Access control: suspended project, cross-project, cross-user isolation
# ---------------------------------------------------------------------

def test_suspended_project_blocks_fallback_video_route(client, db_session, project_with_pair):
    project, _pair = project_with_pair
    project.is_active = False
    db_session.commit()

    resp = client.get(f"/api/scanner/{project.id}/fallback-video")
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "PROJECT_UNAVAILABLE"


def test_suspended_project_blocks_fallback_event_route(client, db_session, project_with_pair):
    project, _pair = project_with_pair
    project.is_active = False
    db_session.commit()

    resp = client.post(
        f"/api/scanner/{project.id}/fallback-event",
        json={"event_type": "recognition_timeout", "client_event_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "PROJECT_UNAVAILABLE"


def test_cross_project_pair_index_never_leaks_another_projects_video(
    client, app_module, db_session, plan, project_with_pair
):
    project, _pair = project_with_pair
    _other_user, other_project, other_pair = _other_user_project_with_pair(app_module, db_session, plan, index=7)

    # project (A) has no pair_index=7 of its own - requesting it against A
    # must never resolve to project B's pair, even though pair_index=7 is a
    # perfectly valid index under B.
    resp = client.get(f"/api/scanner/{project.id}/fallback-video?pair_index=7")
    assert resp.get_json() == {"available": False}

    # And directly confirm B's pair id can't be smuggled in as A's project
    # default either.
    project.fallback_pair_id = other_pair.id
    db_session.commit()
    resp2 = client.get(f"/api/scanner/{project.id}/fallback-video")
    assert resp2.get_json() == {"available": False}


def test_set_fallback_pair_rejects_pair_belonging_to_a_different_project(
    client, app_module, db_session, normal_user, plan, project_with_pair
):
    project, _pair = project_with_pair
    _other_user, _other_project, other_pair = _other_user_project_with_pair(app_module, db_session, plan, index=3)

    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id

    resp = client.post(f"/project/{project.id}/fallback-pair", json={"pair_index": other_pair.pair_index})
    # other_pair.pair_index (3) doesn't exist on `project`, so this 404s
    # rather than ever attaching a foreign project's pair as the fallback.
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "PAIR_NOT_FOUND"
    assert app_module.Project.query.get(project.id).fallback_pair_id is None


def test_set_fallback_pair_rejects_non_owner(client, app_module, db_session, plan, project_with_pair):
    project, _pair = project_with_pair
    other_user, _other_project, _other_pair = _other_user_project_with_pair(app_module, db_session, plan, index=0)

    with client.session_transaction() as sess:
        sess["user_id"] = other_user.id

    resp = client.post(f"/project/{project.id}/fallback-pair", json={"pair_index": 0})
    assert resp.status_code == 404
    assert app_module.Project.query.get(project.id).fallback_pair_id is None


def test_owner_can_set_and_clear_project_fallback_pair(client, app_module, db_session, normal_user, project_with_pair):
    project, pair = project_with_pair
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id

    resp = client.post(f"/project/{project.id}/fallback-pair", json={"pair_index": pair.pair_index})
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    assert app_module.Project.query.get(project.id).fallback_pair_id == pair.id

    clear = client.post(f"/project/{project.id}/fallback-pair", json={"pair_index": None})
    assert clear.status_code == 200
    assert app_module.Project.query.get(project.id).fallback_pair_id is None
