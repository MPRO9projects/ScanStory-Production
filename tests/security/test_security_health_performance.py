import logging
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

from rate_limit import InMemoryRateLimiter


pytestmark = pytest.mark.security


@pytest.fixture(autouse=True)
def clear_request_limiter(app_module):
    app_module.request_limiter.clear()
    yield
    app_module.request_limiter.clear()


def test_healthz_is_minimal_and_ready_checks_database(client, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "fake")
    monkeypatch.delenv("SCANSTORY_QUEUE_REQUIRED", raising=False)
    monkeypatch.delenv("SCANSTORY_PRODUCTION", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.get_json() == {"status": "ok"}
    assert health.headers["Cache-Control"] == "no-store"

    ready = client.get("/ready")
    assert ready.status_code == 200
    # Wave 1 P0-6: /ready now reports the resolved queue mode. The test runtime
    # is non-production, so fake mode remains ready - just no longer silent.
    payload = ready.get_json()
    assert payload["status"] == "ready"
    assert payload["checks"]["database"] == "ok"
    assert ready.headers["Cache-Control"] == "no-store"


def test_ready_failure_is_generic(client, app_module, monkeypatch):
    def fail_readiness():
        raise RuntimeError("secret database path F:/private/prod.db")

    monkeypatch.setattr(app_module, "_readiness_checks", fail_readiness)
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "not_ready",
        "checks": {"database": "unavailable"},
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert "secret database path" not in response.get_data(as_text=True)


def test_process_local_limiter_prunes_stale_buckets_and_enforces_key_bound():
    now = [100.0]
    limiter = InMemoryRateLimiter(clock=lambda: now[0], max_keys=2)

    assert limiter.check("one", 1, 10) == (True, 0)
    assert limiter.check("two", 1, 10) == (True, 0)
    assert limiter.check("three", 1, 10) == (True, 0)
    assert len(limiter._events) == 2

    now[0] = 111.0
    assert limiter.check("fresh", 1, 10) == (True, 0)
    assert list(limiter._events) == ["fresh"]


def test_scanner_detect_init_is_rate_limited_without_blocking_distinct_sessions(
    client, app_module, monkeypatch
):
    monkeypatch.setitem(app_module.RATE_LIMITS, "scanner_init", (1, 60))

    first = client.post("/detect_init", data={"project_id": "1", "scan_session_id": "same"})
    second = client.post("/detect_init", data={"project_id": "1", "scan_session_id": "same"})

    assert first.status_code != 429
    assert second.status_code == 429
    assert second.get_json()["code"] == "RATE_LIMITED"
    assert second.headers["Retry-After"].isdigit()

    app_module.request_limiter.clear()
    monkeypatch.setitem(app_module.RATE_LIMITS, "scanner_init", (3, 60))
    for session_id in ("viewer-a", "viewer-b", "viewer-c"):
        response = client.post(
            "/detect_init",
            data={"project_id": "1", "scan_session_id": session_id},
        )
        assert response.status_code != 429


def test_scanner_track_and_session_end_are_rate_limited(client, app_module, monkeypatch):
    monkeypatch.setitem(app_module.RATE_LIMITS, "scanner_track", (1, 60))
    first_track = client.post("/detect_track", data={"project_id": "1", "pair_id": "0", "scan_session_id": "same"})
    second_track = client.post("/detect_track", data={"project_id": "1", "pair_id": "0", "scan_session_id": "same"})

    assert first_track.status_code != 429
    assert second_track.status_code == 429
    assert second_track.get_json()["code"] == "RATE_LIMITED"

    monkeypatch.setitem(app_module.RATE_LIMITS, "scanner_session_end", (1, 60))
    first_end = client.post("/api/scanner/session/end", json={"project_id": 1, "session_id": "same"})
    second_end = client.post("/api/scanner/session/end", json={"project_id": 1, "session_id": "same"})

    assert first_end.status_code != 429
    assert second_end.status_code == 429
    assert second_end.get_json()["code"] == "RATE_LIMITED"


def test_upload_rate_limit_is_per_authenticated_user(client, app_module, login_user, monkeypatch):
    monkeypatch.setitem(app_module.RATE_LIMITS, "upload", (1, 3600))

    first = client.post("/upload", data={"name": "First attempt"}, follow_redirects=False)
    second = client.post("/upload", data={"name": "Second attempt"}, follow_redirects=False)

    assert first.status_code == 302
    assert second.status_code == 302
    with client.session_transaction() as sess:
        flashed = [message for _category, message in sess.get("_flashes", [])]
    assert "Too many upload attempts. Please wait before starting another upload." in flashed


def test_auth_ip_throttle_spans_accounts_but_allows_normal_shared_ip_traffic(
    client, app_module, db_session, normal_user, monkeypatch
):
    second_user = app_module.User(
        email="second-user@example.com",
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_status="trial",
    )
    db_session.add(second_user)
    db_session.commit()

    monkeypatch.setitem(app_module.RATE_LIMITS, "login_ip", (2, 900))
    for email in (normal_user.email, second_user.email):
        response = client.post("/login/", data={"email": email, "password": "wrong"})
        assert response.status_code == 200

    blocked = client.post("/login/", data={"email": normal_user.email, "password": "wrong"})
    assert blocked.status_code == 429

    app_module.request_limiter.clear()
    monkeypatch.setitem(app_module.RATE_LIMITS, "login_ip", (5, 900))
    for email in (normal_user.email, second_user.email):
        response = client.post("/login/", data={"email": email, "password": "password123"})
        assert response.status_code == 302
        client.get("/logout/")


def test_csp_allows_only_required_external_image_origin(app_module):
    img_sources = app_module._CSP_DIRECTIVES["img-src"]
    assert "https://images.pexels.com" in img_sources
    assert "https://via.placeholder.com" not in img_sources
    assert "https:" not in img_sources
    assert "*" not in img_sources
    assert "via.placeholder.com" not in Path("templates/user/project_preview.html").read_text(encoding="utf-8")
    assert "via.placeholder.com" not in Path("templates/admin/project_preview.html").read_text(encoding="utf-8")


def test_media_cache_headers_preserve_range_and_suspension_blocks_access(
    client, app_module, db_session, project_with_pair
):
    project, pair = project_with_pair

    response = client.get(f"/video/{project.id}/{pair.pair_index}", headers={"Range": "bytes=0-3"})
    assert response.status_code == 206
    assert response.headers["Content-Range"].startswith("bytes 0-3/")
    assert "public" in response.headers["Cache-Control"]
    assert "no-store" not in response.headers["Cache-Control"]

    image = client.get(f"/image/{project.id}/{pair.pair_index}")
    assert image.status_code == 200
    assert "public" in image.headers["Cache-Control"]

    qr = client.get(f"/qr/{project.qr_code_filename}")
    assert qr.status_code == 200
    assert "public" in qr.headers["Cache-Control"]

    project.is_active = False
    db_session.commit()
    suspended = client.get(f"/video/{project.id}/{pair.pair_index}")
    assert suspended.status_code == 404
    # Copy is deliberately generic (never says "suspended" specifically) so an
    # unavailable response can't be used to enumerate WHY a project is down -
    # see _project_unavailable_response()'s own comment.
    assert b"This experience is unavailable" in suspended.data


def test_admin_media_uses_private_cache_not_public(client, app_module, db_session, admin):
    project = app_module.Project(
        name="Admin Cache Project",
        owner_admin_id=admin.id,
        qr_code_filename="project_1_admin.png",
    )
    db_session.add(project)
    db_session.commit()

    image_path = Path(app_module.ADMIN_IMAGES_DIR) / f"{project.id}_0.jpg"
    video_path = Path(app_module.ADMIN_VIDEOS_DIR) / f"{project.id}_0.mp4"
    qr_path = Path(app_module.ADMIN_QR_DIR) / "project_1_admin.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    qr_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"fake image")
    video_path.write_bytes(b"fake video")
    qr_path.write_bytes(b"fake qr")

    pair = app_module.ProjectPair(
        project_id=project.id,
        pair_index=0,
        image_filename=image_path.name,
        video_filename=video_path.name,
        is_processed=True,
    )
    db_session.add(pair)
    db_session.commit()

    for path in (
        f"/admin/image/{project.id}/0",
        f"/admin/video/{project.id}/0",
        "/admin/qr/project_1_admin.png",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "private, max-age=3600"


def test_opencv_static_assets_are_long_cached_and_service_worker_is_narrow(client):
    for path in ("/static/js/opencv.js", "/static/js/opencv_js.wasm"):
        response = client.head(path)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"

    sw_response = client.get("/static/sw.js")
    assert sw_response.status_code == 200
    assert sw_response.headers["Service-Worker-Allowed"] == "/"
    assert sw_response.headers["Cache-Control"] == "no-cache"

    sw_source = Path("static/sw.js").read_text(encoding="utf-8")
    assert "/static/js/opencv.js" in sw_source
    assert "/static/js/opencv_js.wasm" in sw_source
    assert "url.pathname.startsWith('/static/js/opencv')" in sw_source
    assert "fetch(event.request)" in sw_source

    scanner_source = Path("templates/user/scanner.html").read_text(encoding="utf-8")
    assert "navigator.serviceWorker.register('/static/sw.js', { scope: '/' })" in scanner_source
    assert ".catch(function (err)" in scanner_source


def test_scanner_latency_log_is_structured_and_safe(client, app_module, monkeypatch, caplog):
    monkeypatch.setitem(app_module.RATE_LIMITS, "scanner_init", (1, 60))

    with caplog.at_level(logging.INFO, logger=app_module.app.logger.name):
        client.post("/detect_init", data={"project_id": "7", "scan_session_id": "safe-session"})
        response = client.post("/detect_init", data={"project_id": "7", "scan_session_id": "safe-session"})

    assert response.status_code == 429
    records = [record for record in caplog.records if hasattr(record, "scanner_latency")]
    assert records
    payload = records[-1].scanner_latency
    assert payload["event"] == "detect_init"
    assert payload["project_id"] == 7
    assert payload["outcome"] == "rate_limited"
    assert "duration_ms" in payload
    serialized = str(payload)
    assert "user@example.com" not in serialized
    assert "cookie" not in serialized.lower()


def test_payment_order_log_no_longer_logs_email_bearing_payload():
    source = Path("app.py").read_text(encoding="utf-8")
    assert "Creating Razorpay order: {order_data}" not in source
    log_start = source.index('"Creating Razorpay order"')
    log_block = source[log_start:log_start + 500]
    assert "user_email" not in log_block
    assert "user.id" in log_block


def test_landing_page_has_reduced_motion_rule():
    source = Path("templates/user/landing.html").read_text(encoding="utf-8")
    assert "@media (prefers-reduced-motion: reduce)" in source
    assert "animation-duration: 0.01ms !important" in source
    assert "[data-aos]" in source
    assert "transform: none !important" in source
