"""V1.1 final production-ops + admin-investigation certification.

Two halves, matching the two defect families this lane closed:

* PRODUCTION OPS - bounded dependency probes, the shared-Redis rate limiter's
  outage policy, secret-free readiness output, and upload-session cleanup
  including the crashed-finalize recovery that previously had no path out.
* ADMIN INVESTIGATION - the Admin > Users > View User > project > evidence
  chain, and the moderation queue's investigation context.

Everything here is a regression guard on behaviour that was measured to be
broken (or measured to be already correct and worth pinning), not a restatement
of coverage that already exists in test_wave1_p0_blockers.py (shared-Redis
counter semantics), test_v11_p1_backend_security_ops.py (worker-aware
readiness), test_resumable_upload.py / test_multi_pair_resumable_upload.py
(expire sweep) or test_domain_commercial_capacity_and_reporting.py (report
submission + moderation transitions).
"""
from datetime import datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash

import rate_limit
from public_keys import generate_unique_public_key


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _make_user(app_module, db_session, email, *, active=True):
    user = app_module.User(
        email=email,
        first_name=email.split("@")[0],
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_status="active" if active else "expired",
        subscription_expires_at=(
            datetime.utcnow() + timedelta(days=30) if active
            else datetime.utcnow() - timedelta(days=1)
        ),
        subscribed_project_limit=3,
        subscribed_scan_limit=100,
        projects_used=0,
        scans_used=0,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _make_project_with_media(app_module, db_session, owner, *, name="Evidence Project", active=True, index=1):
    from pathlib import Path

    project = app_module.Project(
        name=name,
        owner_user_id=owner.id,
        created_by_user_id=owner.id,
        current_owner_user_id=owner.id,
        user_project_index=index,
        scanner_url="/scanner/evidence",
        qr_code_filename="project_evidence_main.png",
        qr_code_path="/qr/project_evidence_main.png",
        is_active=active,
    )
    db_session.add(project)
    db_session.commit()

    image_path = Path(app_module.IMAGES_DIR) / f"{project.id}_0.jpg"
    video_path = Path(app_module.VIDEOS_DIR) / f"{project.id}_0.mp4"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"marker image bytes")
    video_path.write_bytes(b"overlay video bytes")

    pair = app_module.ProjectPair(
        project_id=project.id,
        pair_index=0,
        image_filename=image_path.name,
        video_filename=video_path.name,
        image_path=f"/image/{project.id}/0",
        is_processed=True,
        processing_status="completed",
        feature_extraction_status="extracted",
    )
    db_session.add(pair)
    db_session.commit()
    return project, pair


def _login_admin(client, admin_obj):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin_obj.id


def _make_report(app_module, db_session, project, **kwargs):
    report = app_module.ContentReport(
        project_id=project.id,
        reason=kwargs.pop("reason", "SPAM"),
        status=kwargs.pop("status", "OPEN"),
        **kwargs,
    )
    db_session.add(report)
    db_session.commit()
    return report


class _FakeRedis:
    """Minimal INCR/TTL/EXPIRE surface, enough for limiter semantics."""

    def __init__(self):
        self.store = {}
        self.ttls = {}

    def pipeline(self):
        return _FakePipeline(self)

    def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def ttl(self, key):
        return self.ttls.get(key, -1)

    def expire(self, key, seconds):
        self.ttls[key] = int(seconds)
        return True


class _FakePipeline:
    def __init__(self, redis):
        self._redis = redis
        self._ops = []

    def incr(self, key):
        self._ops.append(("incr", key))
        return self

    def ttl(self, key):
        self._ops.append(("ttl", key))
        return self

    def execute(self):
        return [getattr(self._redis, op)(key) for op, key in self._ops]


class _HangingRedis:
    """Every operation raises, standing in for an unreachable Redis."""

    def pipeline(self):
        raise OSError("connection refused")


# ===========================================================================
# A. Bounded dependency probes
# ===========================================================================
def test_queue_redis_client_bounds_every_socket_operation(monkeypatch):
    """The defect: redis-py defaults socket_timeout to None, so a Redis that
    accepts a connection and then never answers (firewall DROP, hung server)
    made /ready and every enqueue block forever on exactly the outage the probe
    exists to report."""
    import processing_queue

    captured = {}

    class _Recorder:
        @staticmethod
        def from_url(url, **kwargs):
            captured.update(kwargs)
            captured["url"] = url
            return object()

    monkeypatch.setitem(__import__("sys").modules, "redis", type("m", (), {"Redis": _Recorder}))
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    processing_queue.redis_connection()

    assert captured["socket_timeout"] >= 1
    assert captured["socket_connect_timeout"] >= 1


def test_redis_socket_timeout_is_operator_tunable_and_rejects_garbage(monkeypatch):
    import processing_queue

    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT_SECONDS", "12")
    assert processing_queue.redis_socket_timeout_seconds() == 12
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT_SECONDS", "not-a-number")
    assert processing_queue.redis_socket_timeout_seconds() == 5
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT_SECONDS", "0")
    assert processing_queue.redis_socket_timeout_seconds() == 1


def test_rate_limit_redis_client_bounds_every_socket_operation(monkeypatch):
    """Same hazard on the limiter's own client: a fail-closed policy that never
    returns is a hang, not a policy."""
    captured = {}

    class _Recorder:
        @staticmethod
        def from_url(url, **kwargs):
            captured.update(kwargs)
            return _FakeRedis()

    monkeypatch.setitem(__import__("sys").modules, "redis", type("m", (), {"Redis": _Recorder}))
    limiter = rate_limit.build_limiter(redis_url="redis://localhost:6379/1")

    assert limiter.backend == "redis"
    assert captured["socket_timeout"] >= 1
    assert captured["socket_connect_timeout"] >= 1


def test_readiness_bounds_its_database_statement_on_postgresql(app_module, monkeypatch):
    """connect_timeout bounds REACHING the database; nothing bounded the probe
    statement itself until READINESS_DB_TIMEOUT_MS."""
    import unittest.mock as mock

    statements = []
    monkeypatch.setattr(
        app_module.db.session, "execute", lambda clause, *a, **k: statements.append(str(clause))
    )
    postgres_dialect = type("_Dialect", (), {"name": "postgresql"})()
    with mock.patch.object(
        type(app_module.db), "engine",
        mock.PropertyMock(return_value=mock.Mock(dialect=postgres_dialect)),
    ):
        app_module._readiness_probe_database()

    assert any("statement_timeout" in stmt for stmt in statements), statements
    assert any("SELECT 1" in stmt for stmt in statements), statements
    assert str(app_module.READINESS_DB_TIMEOUT_MS) in " ".join(statements)


def test_readiness_on_sqlite_skips_the_postgres_only_pragma(app_module):
    """SQLite has no SET LOCAL; the probe must still work, which is what the
    whole test suite runs on."""
    app_module._readiness_probe_database()


# ===========================================================================
# B. Health / readiness output safety
# ===========================================================================
def test_healthz_is_alive_only_and_never_touches_a_dependency(client, app_module, monkeypatch):
    def _explode():
        raise AssertionError("/healthz must not probe Redis")

    monkeypatch.setattr(app_module, "redis_ready_check", _explode)
    monkeypatch.setattr(app_module, "queue_worker_state", _explode)

    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_ready_is_machine_readable_and_names_dependencies_generically(client):
    response = client.get("/ready")
    assert response.status_code in (200, 503)
    body = response.get_json()
    assert body["status"] in ("ready", "not_ready")
    assert isinstance(body["checks"], dict)
    assert "database" in body["checks"]


SENTINEL_SECRETS = {
    "DATABASE_URL": "postgresql://sentinel_user:sentinel_pw@sentinel-host:5432/sentinel_db",
    "FLASK_SECRET_KEY": "sentinel-flask-secret-value",
    "RAZORPAY_KEY_SECRET": "sentinel-razorpay-secret",
    "RAZORPAY_WEBHOOK_SECRET": "sentinel-webhook-secret",
    "SMTP_PASS": "sentinel-smtp-password",
    "REDIS_URL": "redis://sentinel_redis_user:sentinel_redis_pw@sentinel-redis:6379/0",
}


@pytest.mark.parametrize("path", ["/healthz", "/ready"])
def test_health_and_readiness_never_echo_a_secret_or_a_stack_trace(client, monkeypatch, path):
    """Dummy sentinels only - never a real secret in a fixture."""
    for key, value in SENTINEL_SECRETS.items():
        monkeypatch.setenv(key, value)

    body = client.get(path).get_data(as_text=True)
    for value in SENTINEL_SECRETS.values():
        assert value not in body
    for fragment in ("sentinel_pw", "sentinel-redis", "Traceback", "File \"", "psycopg", "sqlalchemy.exc"):
        assert fragment not in body


def test_readiness_reports_database_unavailable_without_the_exception_text(client, app_module, monkeypatch):
    def _boom():
        raise RuntimeError("could not connect to " + SENTINEL_SECRETS["DATABASE_URL"])

    monkeypatch.setattr(app_module, "_readiness_checks", _boom)
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.get_data(as_text=True)
    assert response.get_json()["checks"] == {"database": "unavailable"}
    assert "sentinel_pw" not in body and "could not connect" not in body


# ===========================================================================
# C. Shared rate limiter - policy and key safety
# ===========================================================================
def test_two_worker_processes_share_one_redis_budget():
    shared = _FakeRedis()
    worker_a = rate_limit.build_limiter(client=shared)
    worker_b = rate_limit.build_limiter(client=shared)

    assert worker_a.check("login_identity:1.2.3.4:abc", 2, 60)[0] is True
    assert worker_b.check("login_identity:1.2.3.4:abc", 2, 60)[0] is True
    # The third attempt is refused no matter which worker receives it.
    assert worker_b.check("login_identity:1.2.3.4:abc", 2, 60)[0] is False
    assert worker_a.check("login_identity:1.2.3.4:abc", 2, 60)[0] is False


def test_window_ttl_is_set_once_and_is_the_retry_after():
    shared = _FakeRedis()
    limiter = rate_limit.build_limiter(client=shared)
    assert limiter.check("k", 1, 90)[0] is True
    assert shared.ttls["scanstory:rl:k"] == 90

    # Sustained abuse must not keep extending its own window.
    blocked, retry_after = limiter.check("k", 1, 90)
    assert blocked is False
    assert retry_after == 90
    assert shared.ttls["scanstory:rl:k"] == 90


def test_expired_window_resets_the_counter():
    shared = _FakeRedis()
    limiter = rate_limit.build_limiter(client=shared)
    assert limiter.check("k", 1, 60)[0] is True
    assert limiter.check("k", 1, 60)[0] is False
    # TTL elapsed: Redis dropped the key.
    shared.store.pop("scanstory:rl:k")
    shared.ttls.pop("scanstory:rl:k")
    assert limiter.check("k", 1, 60)[0] is True


def test_distinct_keys_do_not_share_a_budget():
    shared = _FakeRedis()
    limiter = rate_limit.build_limiter(client=shared)
    assert limiter.check("login_identity:1.1.1.1:a", 1, 60)[0] is True
    assert limiter.check("login_identity:2.2.2.2:a", 1, 60)[0] is True
    assert limiter.check("login_identity:1.1.1.1:a", 1, 60)[0] is False


def test_redis_outage_fails_closed_with_a_short_retry_after():
    limiter = rate_limit.build_limiter(client=_HangingRedis())
    allowed, retry_after = limiter.check("login_ip:1.2.3.4", 1000, 900)
    assert allowed is False
    assert 1 <= retry_after <= rate_limit.FAIL_CLOSED_RETRY_AFTER


def test_redis_outage_never_surfaces_a_stack_trace_to_the_caller(caplog):
    limiter = rate_limit.build_limiter(client=_HangingRedis())
    with caplog.at_level("ERROR"):
        limiter.check("k", 1, 60)
    logged = caplog.text
    assert "rate_limit_backend_unavailable" in logged
    assert "connection refused" not in logged
    assert "Traceback" not in logged


def test_rate_limit_keys_carry_hashed_identities_never_raw_ones(app_module):
    """The key reaches Redis and logs, so an email address must not be in it."""
    email = "Victim@Example.COM"
    with app_module.app.test_request_context("/", environ_base={"REMOTE_ADDR": "9.9.9.9"}):
        key = app_module._rate_limit_key("login_identity", rate_limit.identity_digest(email))
    assert "victim" not in key.lower()
    assert "@" not in key
    assert rate_limit.identity_digest(email) in key


def test_no_rate_limit_scope_embeds_a_secret_shaped_name(app_module):
    """Scope names become Redis keys. Route-describing names are fine
    ("forgot_password_ip" names an endpoint, it carries no credential); a name
    implying the VALUE is in the key is not."""
    for scope in app_module.RATE_LIMITS:
        lowered = scope.lower()
        for forbidden in ("secret", "token", "otp_code", "signature", "_hash_of_password"):
            assert forbidden not in lowered, scope


def test_configured_thresholds_are_unchanged_for_the_audited_limiters(app_module):
    """Semantics preserved: this lane bounded the socket, it did not retune any
    published limit."""
    assert app_module.RATE_LIMITS["login_ip"] == (80, 900)
    assert app_module.RATE_LIMITS["login_identity"] == (15, 900)
    assert app_module.RATE_LIMITS["upload"] == (8, 3600)
    assert app_module.RATE_LIMITS["content_report"] == (5, 3600)
    assert app_module.RATE_LIMITS["admin_login_ip"] == (20, 900)


def test_local_development_fallback_is_still_the_deterministic_limiter(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_REDIS_URL", raising=False)
    limiter = rate_limit.build_limiter()
    assert limiter.backend == "memory"
    assert limiter.shared is False


# ===========================================================================
# D. Upload-session cleanup, including crashed finalizes
# ===========================================================================
def _session(app_module, db_session, **kwargs):
    now = app_module.get_utc_now()
    defaults = dict(
        owner_user_id=None,
        owner_admin_id=None,
        purpose="project_pair",
        project_name="Cleanup Project",
        image_size=10,
        video_size=10,
        expected_total_size=20,
        current_offset=0,
        status="active",
        storage_token=None,
        expires_at=now + timedelta(minutes=60),
    )
    defaults.update(kwargs)
    if not defaults["storage_token"]:
        import uuid

        defaults["storage_token"] = uuid.uuid4().hex
    row = app_module.UploadSession(**defaults)
    db_session.add(row)
    db_session.commit()
    return row


def _age(app_module, db_session, row, minutes):
    """updated_at carries onupdate=now(), so it is written directly."""
    app_module.UploadSession.query.filter_by(id=row.id).update(
        {app_module.UploadSession.updated_at: app_module.get_utc_now() - timedelta(minutes=minutes)},
        synchronize_session=False,
    )
    db_session.commit()


def test_stuck_finalizing_with_a_project_recovers_to_assembled(app_module, db_session, normal_user):
    """Crash AFTER the project/quota commit. project_id is already set, so only
    QR + enqueue remained - which is exactly what 'assembled' means, and the
    finalize route already retries only that step."""
    project, pair = _make_project_with_media(app_module, db_session, normal_user)
    row = _session(
        app_module, db_session, owner_user_id=normal_user.id, status="finalizing",
        project_id=project.id, pair_id=pair.id, current_offset=20,
    )
    _age(app_module, db_session, row, 500)

    runner = app_module.app.test_cli_runner()
    result = runner.invoke(args=["cleanup-upload-sessions", "--apply"])
    assert result.exit_code == 0
    assert "recover_to_assembled" in result.output
    assert "Finalizing recovered: 1" in result.output
    assert app_module.UploadSession.query.get(row.id).status == "assembled"
    # The durable work is untouched - no duplicate project, no lost pair.
    assert app_module.Project.query.get(project.id) is not None
    assert app_module.ProjectPair.query.filter_by(project_id=project.id).count() == 1


def test_stuck_finalizing_with_intact_bytes_recovers_to_active(app_module, db_session, normal_user):
    """Crash BEFORE validation consumed the assembled file: no project, no
    quota, bytes still there at the declared length. The creator gets the
    transfer back rather than paying for a crash."""
    row = _session(
        app_module, db_session, owner_user_id=normal_user.id, status="finalizing", current_offset=20,
    )
    temp_path = app_module._upload_session_temp_path(row.storage_token)
    with open(temp_path, "wb") as fh:
        fh.write(b"x" * 20)
    _age(app_module, db_session, row, 500)

    result = app_module.app.test_cli_runner().invoke(args=["cleanup-upload-sessions", "--apply"])
    assert "recover_to_active" in result.output
    assert app_module.UploadSession.query.get(row.id).status == "active"
    import os

    assert os.path.exists(temp_path), "recoverable bytes must survive the sweep"


def test_stuck_finalizing_without_recoverable_bytes_fails_honestly(app_module, db_session, normal_user):
    """Crash after validation deleted the assembled file and before the project
    committed: nothing to resume, so 'active' would be a lie."""
    row = _session(
        app_module, db_session, owner_user_id=normal_user.id, status="finalizing", current_offset=20,
    )
    _age(app_module, db_session, row, 500)

    result = app_module.app.test_cli_runner().invoke(args=["cleanup-upload-sessions", "--apply"])
    assert "fail_no_recoverable_bytes" in result.output
    recovered = app_module.UploadSession.query.get(row.id)
    assert recovered.status == "failed"
    assert recovered.failure_code == "FINALIZE_INTERRUPTED"


def test_a_recent_finalizing_session_is_never_touched(app_module, db_session, normal_user):
    """A live finalize is mid-request. Only rows past the threshold are
    candidates."""
    row = _session(
        app_module, db_session, owner_user_id=normal_user.id, status="finalizing", current_offset=20,
    )
    result = app_module.app.test_cli_runner().invoke(args=["cleanup-upload-sessions", "--apply"])
    assert "Stuck finalizing sessions found (bounded to limit=" in result.output
    assert "Finalizing recovered: 0" in result.output
    assert app_module.UploadSession.query.get(row.id).status == "finalizing"


def test_finalizing_sweep_dry_run_changes_nothing(app_module, db_session, normal_user):
    row = _session(
        app_module, db_session, owner_user_id=normal_user.id, status="finalizing", current_offset=20,
    )
    _age(app_module, db_session, row, 500)

    result = app_module.app.test_cli_runner().invoke(args=["cleanup-upload-sessions"])
    assert "Mode: dry-run" in result.output
    assert "fail_no_recoverable_bytes" in result.output
    assert "Finalizing recovered" not in result.output
    assert app_module.UploadSession.query.get(row.id).status == "finalizing"


def test_finalizing_sweep_is_idempotent_on_rerun(app_module, db_session, normal_user):
    row = _session(
        app_module, db_session, owner_user_id=normal_user.id, status="finalizing", current_offset=20,
    )
    _age(app_module, db_session, row, 500)
    runner = app_module.app.test_cli_runner()

    assert "Finalizing recovered: 1" in runner.invoke(args=["cleanup-upload-sessions", "--apply"]).output
    second = runner.invoke(args=["cleanup-upload-sessions", "--apply"])
    assert "Finalizing recovered: 0" in second.output
    assert app_module.UploadSession.query.get(row.id).status == "failed"


def test_completed_and_cancelled_sessions_are_never_candidates(app_module, db_session, normal_user):
    done = _session(
        app_module, db_session, owner_user_id=normal_user.id, status="completed",
        current_offset=20, expires_at=app_module.get_utc_now() - timedelta(days=5),
    )
    cancelled = _session(
        app_module, db_session, owner_user_id=normal_user.id, status="cancelled",
        expires_at=app_module.get_utc_now() - timedelta(days=5),
    )
    _age(app_module, db_session, done, 5000)
    _age(app_module, db_session, cancelled, 5000)

    app_module.app.test_cli_runner().invoke(args=["cleanup-upload-sessions", "--apply"])
    assert app_module.UploadSession.query.get(done.id).status == "completed"
    assert app_module.UploadSession.query.get(cancelled.id).status == "cancelled"


def test_finalizing_threshold_is_operator_tunable(app_module, db_session, normal_user):
    row = _session(
        app_module, db_session, owner_user_id=normal_user.id, status="finalizing", current_offset=20,
    )
    _age(app_module, db_session, row, 30)
    runner = app_module.app.test_cli_runner()

    # Default (120m) leaves a 30-minute-old finalize alone.
    assert "Finalizing recovered: 0" in runner.invoke(args=["cleanup-upload-sessions", "--apply"]).output
    # An operator who knows their finalizes are quick can tighten it.
    assert "Finalizing recovered: 1" in runner.invoke(
        args=["cleanup-upload-sessions", "--apply", "--finalizing-stale-minutes", "10"]
    ).output


def test_a_paused_upload_inside_the_recovery_window_survives(app_module, db_session, normal_user):
    """Long-pause compatibility with Phase 2: the inactivity window is the
    24h TTL, not the old two hours."""
    assert app_module.UPLOAD_SESSION_ABANDONED_STALE_MINUTES == app_module.UPLOAD_SESSION_TTL_MINUTES
    row = _session(app_module, db_session, owner_user_id=normal_user.id, current_offset=10)
    _age(app_module, db_session, row, 300)  # 5 hours idle - well past the old 120

    app_module.app.test_cli_runner().invoke(args=["cleanup-upload-sessions", "--apply"])
    assert app_module.UploadSession.query.get(row.id).status == "active"


def test_temp_deletion_outside_the_upload_root_is_refused(app_module, tmp_path):
    outside = tmp_path / "not-in-upload-root.bin"
    outside.write_bytes(b"do not delete me")
    app_module._safe_delete_upload_temp(str(outside))
    assert outside.exists()


def test_stale_processing_job_recovery_is_bounded_and_dry_run_by_default(app_module, db_session, normal_user):
    project, _pair = _make_project_with_media(app_module, db_session, normal_user)
    for n in range(3):
        job = app_module.ProcessingJob(
            public_key=generate_unique_public_key(db_session, app_module.ProcessingJob, "job"),
            idempotency_key=f"stale-batch:{project.id}:{n}",
            job_type="process_project_pairs",
            status="processing",
            project_id=project.id,
            max_attempts=3,
            attempt_count=0,
            queued_at=datetime.utcnow() - timedelta(hours=4),
            last_heartbeat_at=datetime.utcnow() - timedelta(hours=4),
        )
        db_session.add(job)
    db_session.commit()

    runner = app_module.app.test_cli_runner()
    dry = runner.invoke(args=["recover-processing-jobs", "--older-than-minutes", "30"])
    assert "Mode: dry-run" in dry.output
    assert app_module.ProcessingJob.query.filter_by(status="processing").count() == 3

    bounded = runner.invoke(args=["recover-processing-jobs", "--older-than-minutes", "30", "--limit", "1", "--apply"])
    assert "bounded to limit=1" in bounded.output
    assert app_module.ProcessingJob.query.filter_by(status="processing").count() == 2


def test_a_heartbeating_job_is_never_declared_stale(app_module, db_session, normal_user):
    project, _pair = _make_project_with_media(app_module, db_session, normal_user)
    job = app_module.ProcessingJob(
        public_key=generate_unique_public_key(db_session, app_module.ProcessingJob, "job"),
        idempotency_key=f"heartbeating:{project.id}",
        job_type="process_project_pairs",
        status="processing",
        project_id=project.id,
        max_attempts=3,
        attempt_count=0,
        queued_at=datetime.utcnow() - timedelta(hours=9),
        last_heartbeat_at=datetime.utcnow(),
    )
    db_session.add(job)
    db_session.commit()

    result = app_module.app.test_cli_runner().invoke(
        args=["recover-processing-jobs", "--older-than-minutes", "30", "--apply"]
    )
    assert "Stale jobs found (bounded to limit=200): 0" in result.output
    assert app_module.ProcessingJob.query.get(job.id).status == "processing"


# ===========================================================================
# E. Secret-safe logging
# ===========================================================================
def test_upload_telemetry_drops_anything_outside_its_allowlist(app_module, caplog):
    with app_module.app.app_context():
        with caplog.at_level("INFO"):
            app_module._log_upload_timing(
                "upload_session_finalize",
                upload_session_id=7,
                project_id=9,
                authorization="Bearer sentinel-token-value",
                database_url=SENTINEL_SECRETS["DATABASE_URL"],
                password="sentinel-password",
            )
    text = caplog.text
    assert "sentinel-token-value" not in text
    assert "sentinel_pw" not in text
    assert "sentinel-password" not in text


def test_processing_telemetry_drops_anything_outside_its_allowlist(app_module, caplog):
    with app_module.app.app_context():
        with caplog.at_level("INFO"):
            app_module._log_processing_timing(
                "processing_job_finished",
                job_id=1, project_id=2,
                razorpay_key_secret=SENTINEL_SECRETS["RAZORPAY_KEY_SECRET"],
            )
    assert "sentinel-razorpay-secret" not in caplog.text


def test_forgot_password_failure_logs_a_type_not_the_exception_text(client, app_module, db_session, monkeypatch, caplog):
    user = _make_user(app_module, db_session, "reset-log@example.com")

    def _boom(*args, **kwargs):
        raise RuntimeError("SMTP said 535 for sentinel-smtp-password with code 424242")

    monkeypatch.setattr(app_module, "send_reset_password_otp", _boom)
    app_module.request_limiter.clear()

    with caplog.at_level("WARNING"):
        response = client.post("/forgot-password/", data={"email": user.email}, follow_redirects=True)

    assert response.status_code == 200
    assert "forgot_password_otp_dispatch_failed" in caplog.text
    assert "sentinel-smtp-password" not in caplog.text
    assert "424242" not in caplog.text
    app_module.request_limiter.clear()


def test_safe_error_summary_redacts_secret_shaped_query_fragments(app_module):
    summary = app_module.safe_error_summary("failed: token=sentinel-token secret=sentinel-secret")
    assert "sentinel-token" not in summary
    assert "sentinel-secret" not in summary


# ===========================================================================
# F. Admin investigation - user, project, evidence
# ===========================================================================
def test_admin_user_list_and_user_detail_open(client, app_module, db_session, admin):
    user = _make_user(app_module, db_session, "investigate-me@example.com")
    _login_admin(client, admin)

    assert client.get("/admin/users").status_code == 200
    detail = client.get(f"/admin/users/{user.id}")
    assert detail.status_code == 200
    assert user.email in detail.get_data(as_text=True)


def test_admin_user_detail_lists_that_users_projects_with_a_link(client, app_module, db_session, admin):
    """The regression: admin_view_user has always queried `projects`, but the
    whole Projects card in view_user.html was HTML-commented out, so the
    investigation chain simply had no link and the query result was discarded."""
    user = _make_user(app_module, db_session, "owner-with-projects@example.com")
    project, _pair = _make_project_with_media(app_module, db_session, user, name="Reported Story")
    _login_admin(client, admin)

    html = client.get(f"/admin/users/{user.id}").get_data(as_text=True)
    assert "Reported Story" in html
    assert f"/admin/projects/{project.id}" in html


def test_admin_user_detail_never_lists_another_users_project(client, app_module, db_session, admin):
    owner = _make_user(app_module, db_session, "owner-a@example.com")
    other = _make_user(app_module, db_session, "owner-b@example.com")
    _make_project_with_media(app_module, db_session, owner, name="Story Of A")
    _make_project_with_media(app_module, db_session, other, name="Story Of B", index=2)
    _login_admin(client, admin)

    html = client.get(f"/admin/users/{owner.id}").get_data(as_text=True)
    assert "Story Of A" in html
    assert "Story Of B" not in html


def test_admin_project_detail_renders_the_actual_image_and_video_evidence(client, app_module, db_session, admin):
    """The regression: this page showed media FILENAMES as text only, so an
    admin could not see what image the creator used or what video is on it."""
    user = _make_user(app_module, db_session, "evidence-owner@example.com")
    project, pair = _make_project_with_media(app_module, db_session, user)
    _login_admin(client, admin)

    html = client.get(f"/admin/projects/{project.id}").get_data(as_text=True)
    assert f'src="/image/{project.id}/{pair.pair_index}"' in html
    assert f'src="/video/{project.id}/{pair.pair_index}"' in html
    assert "<video" in html


def test_admin_project_detail_links_back_to_the_owning_account(client, app_module, db_session, admin):
    user = _make_user(app_module, db_session, "back-to-owner@example.com")
    project, _pair = _make_project_with_media(app_module, db_session, user)
    _login_admin(client, admin)

    html = client.get(f"/admin/projects/{project.id}").get_data(as_text=True)
    assert f"/admin/users/{user.id}" in html
    assert "/admin/projects" in html


def test_authorized_admin_can_load_evidence_for_a_live_project(client, app_module, db_session, admin):
    user = _make_user(app_module, db_session, "live-evidence@example.com")
    project, pair = _make_project_with_media(app_module, db_session, user)
    _login_admin(client, admin)

    assert client.get(f"/image/{project.id}/{pair.pair_index}").status_code == 200
    assert client.get(f"/video/{project.id}/{pair.pair_index}").status_code == 200


def test_authorized_admin_can_still_load_evidence_after_suspending_the_project(client, app_module, db_session, admin):
    """THE moderation defect: suspending a reported project set is_active=False,
    which made _project_is_available false, which 404'd the marker image and
    video for the ADMIN too. The only way to review the evidence behind a report
    was to re-publish the reported content first."""
    user = _make_user(app_module, db_session, "suspended-evidence@example.com")
    project, pair = _make_project_with_media(app_module, db_session, user)
    project.is_active = False
    db_session.commit()
    _login_admin(client, admin)

    image = client.get(f"/image/{project.id}/{pair.pair_index}")
    video = client.get(f"/video/{project.id}/{pair.pair_index}")
    assert image.status_code == 200
    assert video.status_code == 200
    # Not publicly live, so the bytes must not be cacheable anywhere.
    assert image.headers["Cache-Control"] == "private, no-store"
    assert video.headers["Cache-Control"] == "private, no-store"


def test_the_public_remains_blocked_from_a_suspended_projects_media(client, app_module, db_session, normal_user):
    """The admin bypass must not become a public one."""
    user = _make_user(app_module, db_session, "still-private@example.com")
    project, pair = _make_project_with_media(app_module, db_session, user)
    project.is_active = False
    db_session.commit()

    assert client.get(f"/image/{project.id}/{pair.pair_index}").status_code == 404
    assert client.get(f"/video/{project.id}/{pair.pair_index}").status_code == 404

    # A logged-in ordinary user is still the public for this purpose.
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id
    assert client.get(f"/image/{project.id}/{pair.pair_index}").status_code == 404


def test_live_public_media_keeps_its_ordinary_public_cache(client, app_module, db_session, normal_user):
    project, pair = _make_project_with_media(app_module, db_session, normal_user)
    response = client.get(f"/image/{project.id}/{pair.pair_index}")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=3600"


def test_admin_project_detail_requires_the_projects_view_permission(client, app_module, db_session, admin):
    user = _make_user(app_module, db_session, "perm-check@example.com")
    project, _pair = _make_project_with_media(app_module, db_session, user)

    anonymous = client.get(f"/admin/projects/{project.id}")
    assert anonymous.status_code in (301, 302)
    assert "/admin" in anonymous.headers["Location"]

    _login_admin(client, admin)
    assert client.get(f"/admin/projects/{project.id}").status_code == 200


# ===========================================================================
# G. Report / moderation investigation context
# ===========================================================================
def test_report_queue_and_report_detail_both_load(client, app_module, db_session, admin):
    user = _make_user(app_module, db_session, "queue-owner@example.com")
    project, _pair = _make_project_with_media(app_module, db_session, user)
    report = _make_report(app_module, db_session, project)
    _login_admin(client, admin)

    assert client.get("/admin/moderation").status_code == 200
    listing = client.get("/admin/reports")
    assert listing.status_code == 200
    assert [row["id"] for row in listing.get_json()["reports"]] == [report.id]

    detail = client.get(f"/admin/reports/{report.id}")
    assert detail.status_code == 200
    assert detail.get_json()["report"]["id"] == report.id


def test_report_detail_names_the_exact_reported_project_and_owner(client, app_module, db_session, admin):
    """The regression: the payload carried no owner at all, so a moderator
    decided without knowing which account was being held responsible."""
    owner = _make_user(app_module, db_session, "reported-owner@example.com")
    decoy = _make_user(app_module, db_session, "unrelated@example.com")
    project, _pair = _make_project_with_media(app_module, db_session, owner, name="The Reported One")
    _make_project_with_media(app_module, db_session, decoy, name="Unrelated Story", index=2)
    report = _make_report(app_module, db_session, project, reason="COPYRIGHT_OR_IP")
    _login_admin(client, admin)

    payload = client.get(f"/admin/reports/{report.id}").get_json()["report"]
    assert payload["project_id"] == project.id
    assert payload["project_name"] == "The Reported One"
    assert payload["project_owner_type"] == "user"
    assert payload["project_owner_user_id"] == owner.id
    assert payload["reason"] == "COPYRIGHT_OR_IP"


def test_report_detail_exposes_current_project_state(client, app_module, db_session, admin):
    """"Suspend project" was offered with no way to see it had already been
    applied."""
    owner = _make_user(app_module, db_session, "state-owner@example.com")
    project, _pair = _make_project_with_media(app_module, db_session, owner)
    report = _make_report(app_module, db_session, project)
    _login_admin(client, admin)

    live = client.get(f"/admin/reports/{report.id}").get_json()["report"]
    assert live["project_is_active"] is True
    assert live["project_is_publicly_live"] is True

    project.is_active = False
    db_session.commit()
    suspended = client.get(f"/admin/reports/{report.id}").get_json()["report"]
    assert suspended["project_is_active"] is False
    assert suspended["project_is_publicly_live"] is False


def test_an_authenticated_reporter_is_recorded_and_shown(client, app_module, db_session, admin, normal_user):
    owner = _make_user(app_module, db_session, "known-reporter-owner@example.com")
    project, _pair = _make_project_with_media(app_module, db_session, owner)
    report = _make_report(
        app_module, db_session, project,
        reporter_user_id=normal_user.id, reporter_email=normal_user.email,
    )
    _login_admin(client, admin)

    payload = client.get(f"/admin/reports/{report.id}").get_json()["report"]
    assert payload["reporter_user_id"] == normal_user.id
    assert payload["has_reporter_contact"] is True
    # The identity is an id and a boolean, never the raw address.
    assert normal_user.email not in str(payload)


def test_an_anonymous_report_stays_anonymous_and_invents_no_reporter(client, app_module, db_session, admin):
    owner = _make_user(app_module, db_session, "anon-report-owner@example.com")
    project, _pair = _make_project_with_media(app_module, db_session, owner)
    report = _make_report(app_module, db_session, project)
    _login_admin(client, admin)

    payload = client.get(f"/admin/reports/{report.id}").get_json()["report"]
    assert payload["reporter_user_id"] is None
    assert payload["has_reporter_contact"] is False
    # Privacy hashes are recorded for abuse control but are never handed out.
    assert "reporter_ip_hash" not in payload
    assert "reporter_session_hash" not in payload
    assert "reporter_email" not in payload


def test_report_reason_details_timestamp_and_status_are_all_present(client, app_module, db_session, admin):
    owner = _make_user(app_module, db_session, "detail-owner@example.com")
    project, _pair = _make_project_with_media(app_module, db_session, owner)
    report = _make_report(
        app_module, db_session, project, reason="PRIVACY",
        details="Shows my house number.",
    )
    _login_admin(client, admin)

    payload = client.get(f"/admin/reports/{report.id}").get_json()["report"]
    assert payload["reason"] == "PRIVACY"
    assert payload["details"] == "Shows my house number."
    assert payload["created_at"]
    assert payload["status"] == "OPEN"


def test_the_moderation_page_shows_owner_and_project_state_and_links_to_both(client, app_module, db_session, admin):
    _login_admin(client, admin)
    html = client.get("/admin/moderation").get_data(as_text=True)
    assert "reviewOwner" in html
    assert "reviewProjectState" in html
    assert "/admin/users/" in html
    assert "/admin/projects/" in html


def test_submitting_a_report_never_suspends_the_project_by_itself(client, app_module, db_session):
    """A report is an allegation, not a finding."""
    owner = _make_user(app_module, db_session, "untouched-owner@example.com")
    project, pair = _make_project_with_media(app_module, db_session, owner)
    app_module.request_limiter.clear()

    assert client.post(f"/api/projects/{project.id}/report", json={"reason": "SPAM"}).status_code == 201

    db_session.refresh(project)
    assert project.is_active is True
    assert app_module.Project.query.get(project.id) is not None
    assert app_module.ProjectPair.query.get(pair.id) is not None
    assert app_module._project_is_available(project) is True
    app_module.request_limiter.clear()


def test_moderation_has_no_destructive_action_in_its_vocabulary(app_module):
    """The governed action set is the real invariant - scanning page HTML would
    only catch the sidebar's unrelated Refunds nav link."""
    assert app_module.CONTENT_REPORT_ACTIONS == {
        "NONE", "PROJECT_SUSPENDED", "CREATOR_CONTACT_REQUIRED", "LEGAL_REVIEW_REQUIRED", "OTHER",
    }
    for action in app_module.CONTENT_REPORT_ACTIONS:
        for forbidden in ("DELETE", "BAN", "BLOCK", "REFUND", "PURGE", "REMOVE"):
            assert forbidden not in action, action
    # Suspension is the strongest action and it is reversible by design.
    assert "PROJECT_DELETED" not in app_module.CONTENT_REPORT_ACTIONS


def test_the_moderation_review_form_offers_only_governed_actions(client, app_module, db_session, admin):
    owner = _make_user(app_module, db_session, "vocab-owner@example.com")
    project, _pair = _make_project_with_media(app_module, db_session, owner)
    report = _make_report(app_module, db_session, project)
    _login_admin(client, admin)

    rejected = client.post(
        f"/admin/reports/{report.id}/review",
        json={"status": "ACTION_TAKEN", "resolution_action": "PROJECT_DELETED"},
    )
    assert rejected.status_code == 400
    assert rejected.get_json()["code"] == "INVALID_ACTION"
    assert app_module.ContentReport.query.get(report.id).status == "OPEN"
    assert app_module.Project.query.get(project.id) is not None


def test_review_and_suspend_are_permission_gated(client, app_module, db_session, admin, secondary_admin, monkeypatch):
    owner = _make_user(app_module, db_session, "gated-owner@example.com")
    project, _pair = _make_project_with_media(app_module, db_session, owner)
    report = _make_report(app_module, db_session, project)

    # Unauthenticated cannot mutate.
    assert client.post(f"/admin/reports/{report.id}/review", json={"status": "DISMISSED"}).status_code in (301, 302)
    assert app_module.ContentReport.query.get(report.id).status == "OPEN"

    # An admin whose role lacks the permission cannot either.
    monkeypatch.setitem(
        app_module.ADMIN_ROLE_PERMISSIONS, "admin",
        app_module.ADMIN_ROLE_PERMISSIONS["admin"] - {"admin.reports.manage", "admin.projects.suspend"},
    )
    _login_admin(client, secondary_admin)
    denied = client.post(f"/admin/reports/{report.id}/review", json={"status": "DISMISSED"})
    assert denied.status_code in (301, 302, 403)
    assert app_module.ContentReport.query.get(report.id).status == "OPEN"
    assert client.post(f"/admin/projects/{project.id}/suspend").status_code in (301, 302, 403)
    db_session.refresh(project)
    assert project.is_active is True


def test_dismissing_a_report_leaves_the_project_alone_and_is_audited(client, app_module, db_session, admin):
    owner = _make_user(app_module, db_session, "dismiss-owner@example.com")
    project, _pair = _make_project_with_media(app_module, db_session, owner)
    report = _make_report(app_module, db_session, project)
    _login_admin(client, admin)

    response = client.post(
        f"/admin/reports/{report.id}/review",
        json={"status": "DISMISSED", "resolution_reason": "No policy breach found."},
    )
    assert response.status_code == 200
    stored = app_module.ContentReport.query.get(report.id)
    assert stored.status == "DISMISSED"
    assert stored.reviewed_by_admin_id == admin.id
    assert stored.reviewed_at is not None
    db_session.refresh(project)
    assert project.is_active is True

    audit = app_module.AdminActivity.query.filter_by(activity_type="content_report_review").all()
    assert len(audit) == 1
    assert str(report.id) in audit[0].description


def test_suspending_via_review_suspends_only_and_is_audited(client, app_module, db_session, admin):
    owner = _make_user(app_module, db_session, "suspend-owner@example.com")
    project, pair = _make_project_with_media(app_module, db_session, owner)
    report = _make_report(app_module, db_session, project)
    _login_admin(client, admin)

    response = client.post(
        f"/admin/reports/{report.id}/review",
        json={"status": "ACTION_TAKEN", "resolution_action": "PROJECT_SUSPENDED", "resolution_reason": "Policy breach."},
    )
    assert response.status_code == 200
    db_session.refresh(project)
    assert project.is_active is False
    # Suspension is reversible: nothing was deleted.
    assert app_module.Project.query.get(project.id) is not None
    assert app_module.ProjectPair.query.get(pair.id) is not None
    assert app_module.AdminActivity.query.filter_by(activity_type="content_report_review").count() == 1


def test_suspend_and_restore_are_both_audited_and_reversible(client, app_module, db_session, admin):
    owner = _make_user(app_module, db_session, "restore-owner@example.com")
    project, _pair = _make_project_with_media(app_module, db_session, owner)
    _login_admin(client, admin)

    assert client.post(f"/admin/projects/{project.id}/suspend").status_code in (200, 302)
    db_session.refresh(project)
    assert project.is_active is False

    assert client.post(f"/admin/projects/{project.id}/restore").status_code in (200, 302)
    db_session.refresh(project)
    assert project.is_active is True

    types = {row.activity_type for row in app_module.AdminActivity.query.all()}
    assert "project_suspend" in types
    assert "project_restore" in types


def test_a_report_cannot_be_reopened_to_open(client, app_module, db_session, admin):
    owner = _make_user(app_module, db_session, "reopen-owner@example.com")
    project, _pair = _make_project_with_media(app_module, db_session, owner)
    report = _make_report(app_module, db_session, project)
    _login_admin(client, admin)

    bad = client.post(f"/admin/reports/{report.id}/review", json={"status": "OPEN"})
    assert bad.status_code == 400
    assert app_module.ContentReport.query.get(report.id).status == "OPEN"


def test_creator_and_public_scanner_access_rules_are_unchanged(client, app_module, db_session, normal_user):
    """The admin evidence bypass must not have loosened the creator or public
    contract."""
    project, pair = _make_project_with_media(app_module, db_session, normal_user)
    stranger = _make_user(app_module, db_session, "stranger@example.com")

    # Public can read a live project's media (the scanner depends on it).
    assert client.get(f"/image/{project.id}/{pair.pair_index}").status_code == 200

    # A non-owner still cannot manage the project.
    with client.session_transaction() as sess:
        sess["user_id"] = stranger.id
    assert client.get(f"/project/{project.id}/preview").status_code in (302, 404)
