import logging
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash


pytestmark = pytest.mark.security


@pytest.fixture(autouse=True)
def clear_request_limiter(app_module):
    app_module.request_limiter.clear()
    yield
    app_module.request_limiter.clear()


def test_healthz_is_minimal_and_ready_checks_database(client):
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.get_json() == {"status": "ok"}

    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.get_json() == {"status": "ready", "checks": {"database": "ok"}}


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
    assert "secret database path" not in response.get_data(as_text=True)


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
    assert b"suspended or unavailable" in suspended.data


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
